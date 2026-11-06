"""云端同步：钉钉推来的数据 → 生成 30 个单位页 → 部署到 Netlify。

跑在 GitHub Actions 上，**完全不依赖用户电脑**。数据由钉钉 AI 表格的自动化工作流
（每 10 分钟）以 gzip+base64 塞进 repository_dispatch 的 client_payload。

设计要点：
1. **复用 build_portal 的渲染**，只把数据来源从 dws 换成推送来的 JSON。
   做法是把 `bp.q_all` 换成读本地字典的函数，其余逻辑一行不改——
   页面模板、令牌算法、排序规则、rev 指纹全部与电脑上跑出来的完全一致。
2. **字段形态要转**：FindRecords 输出的是 `{fieldId: value}` 扁平结构，
   dws 的 record query 是 `{recordId, cells:{fieldId: value}}`。关联字段也不一样
   （`{id:[...]}` vs `{linkedRecordIds:[...]}`），这里统一转成 dws 形态。
3. **不打日志里的业务数据**：仓库是公开的，Actions 日志任何人可见。
   只打印 rev / 单量 / 耗时，绝不打印单位名、金额、电话。
4. 人员字段在云端查不到通讯录，直接用推送里的显示名。
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import build_portal as bp  # noqa: E402

API = "https://api.netlify.com/api/v1"


# ------------------------------------------------------------------ 形态转换
def _link(cell):
    """关联字段：{'text': '启明星', 'id': ['rjYM...']} → {'linkedRecordIds': [...]}"""
    if not isinstance(cell, dict):
        return cell
    ids = cell.get("id") or cell.get("ids") or cell.get("linkedRecordIds")
    if isinstance(ids, str):
        ids = [ids]
    if ids:
        return {"linkedRecordIds": list(ids), "name": cell.get("text") or cell.get("name") or ""}
    return {"name": cell.get("text") or cell.get("name") or ""}


def _people(cell, split: bool = False):
    """人员/多值字段：统一成 [{'name': 显示名}]。

    两种坑（2026-11-06 用真实报文确认）：
    1. **多选文本/关联会被压成一个逗号串**：分派单位拿到的是
       `"强记女工,鑫扬帆,启明星"`，直接当单位名用 → 云端只认出一个"单位"，
       30 家真实单位全部 0 单。必须按逗号拆开（split=True）。
    2. **人员字段是一串 JSON 字符串**：`["{\\"staffId\\":\\"148133896\\",
       \\"nickName\\":\\"包波罗\\"}"]`，要取 nickName，不能取整串。
    """
    if cell is None:
        return []
    if isinstance(cell, str):
        s = cell.strip()
        if s[:1] in "{[":
            try:
                return _people(json.loads(s), split)
            except Exception:  # noqa: BLE001  不是合法 JSON，按普通文本处理
                pass
        if split:
            parts = [p.strip() for p in re.split(r"[,，、;；]", s)]
            return [{"name": p} for p in parts if p]
        return [{"name": s}] if s else []
    if isinstance(cell, dict):
        name = (cell.get("nickName") or cell.get("name")
                or cell.get("text") or "")
        if name:
            return [{"name": str(name)}]
        ids = cell.get("id") or []
        return [{"name": str(i)} for i in ids] if isinstance(ids, list) else []
    if isinstance(cell, list):
        out = []
        for it in cell:
            out.extend(_people(it, split))
        return out
    return []


def to_dws(records) -> list[dict]:
    """FindRecords 输出 → dws record query 的形态（build_portal 认得的那种）。"""
    if isinstance(records, str):
        records = json.loads(records)
    out = []
    for i, r in enumerate(records or []):
        if not isinstance(r, dict):
            continue
        cells = {}
        for k, v in r.items():
            if k in ("rowId", "rowUrl"):
                continue
            cells[k] = v
        # 特定字段的形态修正（key 就是 fieldId，直接按需转）
        if bp.F_L_UNIT in cells:
            cells[bp.F_L_UNIT] = _link(cells[bp.F_L_UNIT])
        # 分派单位是**多值**，会被压成逗号串，必须拆开（split=True）
        if bp.F_APP_UNITS in cells:
            cells[bp.F_APP_UNITS] = _people(cells[bp.F_APP_UNITS], split=True)
        for m in bp.F_L_LINES.values():
            if m["cat"] in cells:
                cells[m["cat"]] = _link(cells[m["cat"]])
        # 申请人：FindRecords 给的是 [{nickName, staffId}] 的 JSON 串，
        # 塞进 userId 让 uid_of 取用（云端查不到通讯录，直接用显示名）
        if bp.F_APP_APPLICANT in cells:
            who = _people(cells[bp.F_APP_APPLICANT])
            if who and who[0].get("name"):
                cells[bp.F_APP_APPLICANT] = {"userId": who[0]["name"]}
        out.append({"recordId": r.get("rowId") or f"r{i}", "cells": cells})
    return out


def _relink(list_rows: list[dict], unit_rows: list[dict], cat_rows: list[dict]) -> None:
    """把「按名称」的关联改成「按 recordId」，喂给 build_portal 那套关联逻辑。

    ⚠ 这是云端最容易翻车的一处（2026-11-06 实测踩到）：
    `FindRecords` 的输出**不含 rowId**，关联字段只给 `{text: "启明星", id: ["行ID"]}`。
    单位表记录同样没有 rowId，于是 build_dataset 里的 `rid2unit` 查不到任何键，
    维修清单行全部被丢弃 —— 页面能打开、30 家都在，但**全部 0 单**，
    而且不报错、rev 照常更新，看着像"数据是空的"。
    所以这里用显示名重建索引，把 linkedRecordIds 换成我们自己分配的 recordId。
    """
    def index_by_name(rows: list[dict], fid: str) -> dict[str, str]:
        m: dict[str, str] = {}
        for r in rows:
            name = bp.text_of(r["cells"].get(fid)).strip()
            if name and name not in m:
                m[name] = r["recordId"]
        return m

    unit_by_name = index_by_name(unit_rows, "58IefVX")
    cat_by_name = index_by_name(cat_rows, "ntrwa1J")

    targets = [(bp.F_L_UNIT, unit_by_name)]
    targets += [(m["cat"], cat_by_name) for m in bp.F_L_LINES.values()]

    for r in list_rows:
        cells = r["cells"]
        for fid, idx in targets:
            cell = cells.get(fid)
            if not isinstance(cell, dict):
                continue
            name = (cell.get("name") or bp.text_of(cell)).strip()
            rid = idx.get(name)
            cells[fid] = {"linkedRecordIds": [rid] if rid else [], "name": name}


def load_payload(raw: str) -> dict:
    """client_payload.data = base64(gzip(json))"""
    blob = base64.b64decode(raw)
    data = json.loads(gzip.decompress(blob).decode("utf-8"))
    return data


# ------------------------------------------------------------------ 构建 + 部署
def build_site(tables: dict) -> dict:
    data = {
        bp.T_APP: to_dws(tables.get("app")),
        bp.T_LIST: to_dws(tables.get("list")),
        bp.T_UNIT: to_dws(tables.get("unit")),
        bp.T_CAT: to_dws(tables.get("cat")),
    }
    _relink(data[bp.T_LIST], data[bp.T_UNIT], data[bp.T_CAT])
    bp.q_all = lambda tid: data.get(tid, [])
    # 云端查不到通讯录；申请单里已带显示名，直接用
    bp.user_name = lambda uid, cache: uid or ""

    ds = bp.build_dataset()
    if not ds["units"]:
        raise SystemExit("推送的数据里没有可用单位，放弃部署")
    bp.write_pages(ds)
    return ds


def make_zip() -> bytes:
    """zip = site/ 全部文件 + 跳转页（gh-pages 的两个文件放在站点根）。

    注意：site/index.html 是门户首页，gh-pages/index.html 是 `?u=` 路由器，
    两者同名但用途不同。这里让**路由器**占根路径（和线上现状一致），
    门户首页改名 _portal.html 保留（不被覆盖也不影响扫码）。
    """
    buf = io.BytesIO()
    gh = ROOT / "gh-pages"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (ROOT / "site").rglob("*"):
            if p.is_file():
                rel = p.relative_to(ROOT / "site").as_posix()
                if rel == "index.html":
                    rel = "_portal.html"
                z.write(p, rel)
        for name in ("index.html", "404.html"):
            f = gh / name
            if f.exists():
                z.write(f, name)
    return buf.getvalue()


def deploy(blob: bytes, site_id: str, token: str) -> str:
    req = urllib.request.Request(
        f"{API}/sites/{site_id}/deploys", data=blob,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/zip"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r).get("id", "")


def main() -> int:
    raw = os.environ.get("PAYLOAD_B64", "").strip()
    if not raw:
        print("缺少 PAYLOAD_B64", file=sys.stderr)
        return 2
    t0 = time.time()
    tables = load_payload(raw)
    ds = build_site(tables)

    token = os.environ.get("NETLIFY_TOKEN", "").strip()
    site_id = (os.environ.get("NETLIFY_SITE_ID", "").strip()
               or (ROOT / "netlify_site.txt").read_text(encoding="utf-8").strip())
    if not token or not site_id:
        print("缺少 NETLIFY_TOKEN / NETLIFY_SITE_ID", file=sys.stderr)
        return 2

    deploy_id = deploy(make_zip(), site_id, token)
    n = sum(u["stats"]["total"] for u in ds["units"])
    print(json.dumps({"rev": ds["rev"], "units": len(ds["units"]), "orders": n,
                      "deploy": deploy_id, "sec": round(time.time() - t0, 1)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
