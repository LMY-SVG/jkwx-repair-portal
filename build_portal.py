#!/usr/bin/env python3
"""维修单位扫码门户：钉钉 AI 表格 → 每家维修单位一个手机页 + 二维码。

数据来源（钉钉 AI 表格「维修申报」Base）：
  申请单 gWiKpbQ     —— 分派维修单位（多选）、审批编号、校区、地址、申请人、电话、维修申报信息
  维修清单 hERWDMS   —— 审批编号 + 维修单位名称（关联）→ 是否已维修、金额、明细、完成日期
  维修单位 x6A7uhs   —— 单位名 ↔ recordId
  维修品类 D9FRc59   —— 品类名

联动口径（实测确认）：
  同一审批编号 + 同一维修单位 = 一条维修清单行。多单位单会在清单里落成多行，
  故「(审批编号, 单位)」是唯一可判「这家单位是否已报单/已维修」的键。

用法：
  python build_portal.py            # 拉数据 + 出页面 + 出全部单位二维码
  python build_portal.py --unit 强记女工   # 只出这一家的页面与二维码
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, r"C:\Users\17514\.workbuddy\skills\repair-qr-dispatch\scripts")
try:
    from _dws import DwsError, run as dws_run  # noqa: E402
except Exception:  # noqa: BLE001
    # 云端（GitHub Actions / Linux）没有 dws，也不需要：
    # cloud_sync.py 会把 q_all 换掉，数据来自钉钉工作流推送。
    # 这里给个占位，保证本模块能被 import、页面模板和渲染逻辑可复用。
    class DwsError(Exception):
        pass

    def dws_run(*_a, **_k):
        raise DwsError("当前环境没有 dws（云端模式请走 cloud_sync.py）")

BASE = "dQPGYqjpJYg0E1RXU9102E0MWakx1Z5N"
T_APP, T_LIST, T_UNIT, T_CAT = "gWiKpbQ", "hERWDMS", "x6A7uhs", "D9FRc59"

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA = ROOT / "data"
QRDIR = ROOT / "qr"

# 申请单字段
F_APP_NO = "3Q64l6H"
F_APP_UNITS = "kgzN9LD"
F_APP_INFO = "mLOs0Jb"
F_APP_AREA = "Ua9U9Xe"
F_APP_ADDR = "axqE1Ff"
F_APP_APPLICANT = "xzhAiNN"
F_APP_PHONE = "poDlvYt"
F_APP_APPLY_DATE = "PsxHvQG"
F_APP_DISPATCH_DATE = "OkPCGuq"
F_APP_QRURL = "WR1A5Do"

# 清单字段
F_L_NO = "nvxwOod"
F_L_UNIT = "E3hr9GN"
F_L_TOTAL = "hxswFgC"
F_L_TAX = "UsEbrlq"
F_L_REMOTE = "aXXxSpd"
F_L_DONE_DATE = "QJne8xd"
F_L_FILL_TIME = "CpA01Q5"
F_L_SIGN = "u8c0s3D"
F_L_INFO = "zaGEeJT"
F_L_AREA = "JUrNPpw"
F_L_LINES = {
    1: dict(cat="IlGvwxU", qty="XLRfS2z", price="qp1Ubwn", labor="K1aXpUa",
            amount="ADfhFow", loc="ZLWea6c", desc="RsXAq1y"),
    2: dict(cat="fEMZcjb", qty="kwyxqs2", price="RRD42t1", labor="jJNJwVl",
            amount="rQOgKI5", loc="HAwKPXm", desc="caWKkoM"),
    3: dict(cat="V9Tt2rh", qty="HvmHqgw", price="zoCyd6m", labor="fVxjNAP",
            amount="toIeZ3x", loc="v0YKBa4", desc="w5mT8G9"),
}


# ------------------------------------------------------------------ 拉数
def _dws_retry(*args: str, tries: int = 8, timeout: int = 120) -> dict:
    """调 dws，失败退避重试。

    钉钉 MCP 网关会偶发 `context canceled`（第 4 张表最容易撞上），
    一次抖动就让整轮构建失败、数据停在旧版本。服务是长期跑的，
    必须把这种瞬时错误吸收掉，而不是把失败传给扫码的人。

    只对**网络/取消类**错误重试；参数错、权限错重试也没用，直接抛。

    特别地：**错误消息为空**也要重试。实测 dws 底下的 node 进程偶发崩掉，
    返回 rc!=0 但 stdout/stderr 全空（异常信息里只剩 "dws 执行失败（…）：" 这一行）。
    这种崩溃不是参数问题——参数错一定会带回服务端原文——纯属瞬时故障。

    ## 为什么是 8 次、退避到 30 秒（2026-11-05 现场数据）

    实测 11:23 那一轮：5 次重试（1.2/2.4/3.6/4.8 秒，共约 12 秒）**全部失败**，
    整轮构建报废，扫码页停在旧数据。而 12 分钟后同样的调用连跑 12 次全成功。
    说明网关抖动会持续**几十秒**量级，12 秒的总预算根本不够。
    改成 8 次、退避 1/2/4/8/15/20/30 秒，总预算约 90 秒，能扛过绝大多数抖动。
    代价是极端情况下一次构建变慢，但这远好于「数据停更」。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            return dws_run(*args, timeout=timeout)
        except DwsError as e:
            msg = str(e)
            # 冒号后面空无一物 = 子进程崩了，没有可用归因信息。
            # 现在用 DwsError.silent_crash 属性判定，不再从文本里切——
            # 早先 `msg.split("：", 1)[-1].strip()` 的写法一旦消息里补上
            # rc 字样就立刻失效（静默崩溃被当成不可重试，整轮直接废掉）。
            silent_crash = getattr(e, "silent_crash", False) or \
                len(msg.split("：", 1)[-1].strip()) < 8
            transient = silent_crash or any(k in msg for k in (
                "context canceled", "context deadline", "timeout", "timed out",
                "connection reset", "EOF", "502", "503", "504", "internal",
                "socket hang up", "ECONNRESET", "ETIMEDOUT", "EAI_AGAIN"))
            if not transient or i == tries - 1:
                raise
            last = e
            wait = min(30.0, 1.0 * (2 ** i))
            time.sleep(wait)
    raise last  # pragma: no cover


def q_all(table_id: str) -> list[dict]:
    """分页拉全表（接口单页上限 100，超出报 INVALID_LIMIT）。"""
    out: list[dict] = []
    cursor = None
    while True:
        args = ["aitable", "record", "query", "--base-id", BASE,
                "--table-id", table_id, "--page-size", "100"]
        if cursor:
            args += ["--cursor", cursor]
        page = _dws_retry(*args)["data"]
        out.extend(page.get("records") or [])
        cursor = page.get("nextCursor")
        if not cursor or not page.get("records"):
            break
    return out


def text_of(cell) -> str:
    """把各种单元格形态压成干净字符串。"""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, dict):
        if cell.get("value"):
            v = cell["value"]
            return (v[0] if isinstance(v, list) and v else str(v)).strip()
        for k in ("name", "text", "title"):
            if cell.get(k):
                return str(cell[k]).strip()
        return ""
    if isinstance(cell, list):
        return "、".join(text_of(x) for x in cell if text_of(x))
    return str(cell)


def num_of(cell) -> float | None:
    """金额/数字：去掉 ¥ 、千分位，转 float；空返回 None。"""
    s = text_of(cell).replace("¥", "").replace(",", "").replace("￥", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"¥{v:,.2f}"


def day_of(cell) -> str:
    s = text_of(cell)
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else s


def parse_items(raw: str) -> list[dict]:
    """把「维修申报信息」拆成条目列表。"""
    if not raw:
        return []
    items = []
    for block in re.split(r"【第\d+条】", raw):
        block = block.strip()
        if not block:
            continue
        rec: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or "：" not in line:
                continue
            k, v = line.split("：", 1)
            rec[k.strip()] = v.strip()
        if rec:
            items.append({
                "name": rec.get("需维修物资名称", ""),
                "level": rec.get("维修紧急程度", ""),
                "qty": rec.get("需维修物资数量", ""),
                "loc": rec.get("需维修物资位置", ""),
                "desc": rec.get("问题描述说明", ""),
                "media": rec.get("问题照片或视频上传", ""),
            })
    return items


def urgency_of(level: str) -> tuple[str, str]:
    """紧急程度 → (标签, 配色类)。"""
    if "已维修" in level:
        return "已自行维修", "gray"
    if "紧急" in level:
        return "紧急", "red"
    if "常规" in level:
        return "常规", "blue"
    return (level[:6] or "未标注"), "gray"


def user_name(uid: str, cache: dict[str, str]) -> str:
    if not uid:
        return ""
    if uid in cache:
        return cache[uid]
    try:
        res = _dws_retry("contact", "user", "get", "--ids", uid, tries=2, timeout=30)
        rows = res.get("result") or []
        name = (rows[0].get("orgEmployeeModel") or {}).get("orgUserName", "") if rows else ""
    except DwsError:
        name = ""
    cache[uid] = name or uid
    return cache[uid]


def uid_of(cell) -> str:
    if isinstance(cell, list) and cell:
        first = cell[0]
        if isinstance(first, dict):
            return str(first.get("userId") or "")
    if isinstance(cell, dict):
        return str(cell.get("userId") or "")
    return ""


# ------------------------------------------------------------------ 组装
def build_dataset(only_unit: str | None = None) -> dict:
    apps = q_all(T_APP)
    lists = q_all(T_LIST)
    units = q_all(T_UNIT)
    cats = q_all(T_CAT)

    rid2unit = {r["recordId"]: text_of(r["cells"].get("58IefVX")) for r in units}
    # 维修单位表的全集（表内顺序），去重去空。
    # 页面与二维码都以它为准：30 家一个不少，没单的也要有页面。
    all_unit_names: list[str] = []
    for r in units:
        n = rid2unit.get(r["recordId"], "")
        if n and n not in all_unit_names:
            all_unit_names.append(n)
    rid2cat = {}
    for r in cats:
        c = r["cells"]
        rid2cat[r["recordId"]] = (text_of(c.get("ntrwa1J"))
                                  or "-".join(x for x in (text_of(c.get("mpxNFd1")),
                                                          text_of(c.get("k5CU5Z8")),
                                                          text_of(c.get("OGlSARK"))) if x))

    # (审批编号, 单位名) → 清单行
    bills: dict[tuple[str, str], list[dict]] = {}
    for r in lists:
        c = r["cells"]
        no = text_of(c.get(F_L_NO))
        linked = (c.get(F_L_UNIT) or {}).get("linkedRecordIds") or []
        for rid in linked:
            name = rid2unit.get(rid, "")
            if not name or not no:
                continue
            lines = []
            for i in (1, 2, 3):
                m = F_L_LINES[i]
                cat_ids = (c.get(m["cat"]) or {}).get("linkedRecordIds") or []
                cat = rid2cat.get(cat_ids[0], "") if cat_ids else ""
                qty, price = num_of(c.get(m["qty"])), num_of(c.get(m["price"]))
                labor, amount = num_of(c.get(m["labor"])), num_of(c.get(m["amount"]))
                if not any([cat, qty, price, amount]):
                    continue
                lines.append({
                    "cat": cat, "qty": qty, "price": price,
                    "labor": labor, "amount": amount,
                    "loc": text_of(c.get(m["loc"])), "desc": text_of(c.get(m["desc"])),
                })
            bills.setdefault((no, name), []).append({
                "fillTime": text_of(c.get(F_L_FILL_TIME))[:16].replace("T", " "),
                "doneDate": day_of(c.get(F_L_DONE_DATE)),
                "total": num_of(c.get(F_L_TOTAL)),
                "tax": num_of(c.get(F_L_TAX)),
                "remote": num_of(c.get(F_L_REMOTE)),
                "signed": bool(c.get(F_L_SIGN)),
                "lines": lines,
            })

    uname_cache: dict[str, str] = {}
    by_unit: dict[str, list[dict]] = {}
    for r in apps:
        c = r["cells"]
        no = text_of(c.get(F_APP_NO))
        assigned = c.get(F_APP_UNITS) or []
        names = [u.get("name") for u in assigned if isinstance(u, dict) and u.get("name")]
        info_raw = text_of(c.get(F_APP_INFO))
        items = parse_items(info_raw)
        applicant = user_name(uid_of(c.get(F_APP_APPLICANT)), uname_cache)
        for name in names:
            if only_unit and name != only_unit:
                continue
            bl = bills.get((no, name)) or []
            mine = [b for b in bl if b["lines"] or b["total"] is not None]
            done = bool(mine) and any(b["doneDate"] for b in mine)
            by_unit.setdefault(name, []).append({
                "no": no,
                "area": text_of(c.get(F_APP_AREA)),
                "address": text_of(c.get(F_APP_ADDR)),
                "applicant": applicant,
                "phone": text_of(c.get(F_APP_PHONE)),
                "applyDate": day_of(c.get(F_APP_APPLY_DATE)),
                "dispatchDate": day_of(c.get(F_APP_DISPATCH_DATE)),
                "items": items,
                "status": "done" if done else "pending",
                "bills": mine,
                "total": sum(b["total"] or 0 for b in mine),
            })

    out_units = []
    # **以「维修单位」表为全集**：30 家全部出页面，哪怕当前一单都没有。
    # 早期版本只遍历 by_unit（有单的单位），结果没单的单位没页面、领不到二维码；
    # 而维修单位是相对固定的资源，二维码应当一次发全，不该等它接到第一单才给。
    for name, orders in by_unit.items():
        orders.sort(key=lambda o: (o["status"] == "done", o["dispatchDate"] or o["applyDate"]), reverse=False)
        out_units.append({
            "name": name,
            "orders": orders,
            "stats": {
                "total": len(orders),
                "done": sum(1 for o in orders if o["status"] == "done"),
                "pending": sum(1 for o in orders if o["status"] == "pending"),
                "amount": sum(o["total"] for o in orders),
            },
        })

    seen = {u["name"] for u in out_units}
    for name in all_unit_names:
        if name in seen or (only_unit and name != only_unit):
            continue
        out_units.append({
            "name": name,
            "orders": [],
            "stats": {"total": 0, "done": 0, "pending": 0, "amount": 0.0},
        })

    # 排序：有理赔单的按单量降序在前；零单的按名称排后面（稳定，不跳来跳去）
    out_units.sort(key=lambda u: (u["stats"]["total"] == 0, -u["stats"]["total"], u["name"]))

    # 内容指纹：只有数据真的变了，rev 才变。
    # 用处：serve.py 靠它判断「要不要让扫码的页面自动刷一下」。
    # 若用时间戳判断，表里没新数据也会每 2 分钟刷一次，白白闪烁。
    fingerprint = json.dumps(out_units, ensure_ascii=False, sort_keys=True)
    rev = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]

    # 「校区老师签字」是 attachment 字段，钉钉自动化工作流的 FindRecords
    # **读不到附件类型**（编译期直接报 unknown_field_id）。云端要显示这个标记，
    # 只能靠本机把「审批编号 → 是否已签字」写成状态文件，再由云端读回来合并。
    # 不写这个文件的话，云端每 10 分钟重部署一次会把标记抹掉、本机再部署又出现，
    # 页面上的「校区已签字」就会忽隐忽现。
    try:
        signed_state = {}
        for (no, _unit), bl in bills.items():
            if no and any(b["signed"] for b in bl):
                signed_state[no] = True
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "signed.json").write_text(
            json.dumps(signed_state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except OSError:
        pass

    # 时间一律按北京时间（UTC+8）打印。GitHub Actions 跑在 UTC，
    # 用裸 datetime.now() 会让供应商在手机上看到"数据更新 06:30"（实际 14:30），
    # 以为数据半天没更新。2026-11-06 实际踩到。
    _now = datetime.now(timezone(timedelta(hours=8)))
    return {
        "generated_at": _now.strftime("%Y-%m-%d %H:%M"),
        "built_at": int(time.time()),
        "rev": rev,
        "units": out_units,
    }


# ------------------------------------------------------------------ 页面
def slugify(name: str, idx: int) -> str:
    """单位名 → 稳定 ASCII 文件名（中文不直接进 URL，便于微信/浏览器兼容）。"""
    return f"u{idx:02d}"


def _salt() -> str:
    """令牌盐值：只从环境变量或本地文件读，**绝不写进代码**。

    本文件会进入公开仓库（GitHub Pages 的构建产物源），如果在源码里写死盐值，
    任何人看仓库就能算出全部 30 家单位的页面地址，「令牌不可枚举」直接失效。
    盐值本身不变（换值 = 全部二维码作废），只是搬家。
    """
    s = os.environ.get("PORTAL_SALT", "").strip()
    if s:
        return s
    f = Path(__file__).resolve().parent / "portal_salt.txt"
    if f.exists():
        s = f.read_text(encoding="utf-8").strip()
        if s:
            return s
    raise RuntimeError(
        "缺少令牌盐值：请设置环境变量 PORTAL_SALT，或在脚本同级放 portal_salt.txt。"
    )


def unit_token(name: str) -> str:
    """单位名 → 稳定令牌。

    页面里含校区地址/申请人电话/维修金额，只靠「网址难猜」防护不够；
    文件名带上不可枚举的令牌，单位之间也无法互看（每家只拿到自己那张二维码）。
    """
    salted = f"{_salt()}{name}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()[:12]


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f5f6f8">
<meta name="rev" content="{rev}">
<title>{unit} · 我的维修单</title>
<style>
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{margin:0;padding:0}}
body{{background:#f2f3f5;color:#1a1d23;font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding-bottom:32px}}
.hd{{background:linear-gradient(135deg,#1f6feb,#3b8bff);color:#fff;padding:20px 18px 18px}}
.hd .sub{{font-size:12px;opacity:.82;letter-spacing:.3px}}
.hd h1{{margin:4px 0 0;font-size:22px;font-weight:700;letter-spacing:.5px}}
.hd .meta{{margin-top:10px;font-size:11.5px;opacity:.78;display:flex;gap:12px;flex-wrap:wrap}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#e6e8ec;margin:0 0 2px}}
.stats div{{background:#fff;padding:12px 6px;text-align:center}}
.stats b{{display:block;font-size:18px;font-weight:700;line-height:1.2}}
.stats span{{font-size:11px;color:#6b7280}}
.b-done b{{color:#0a9d5c}} .b-pend b{{color:#e08300}} .b-amt b{{color:#1f6feb;font-size:15px}}
.wrap{{padding:12px}}
.tabs{{display:flex;gap:8px;margin:0 0 12px}}
.tabs button{{flex:1;border:1px solid #dcdfe4;background:#fff;color:#4b5563;padding:9px 0;border-radius:9px;font-size:13px;font-weight:600}}
.tabs button.on{{background:#1f6feb;border-color:#1f6feb;color:#fff}}
.card{{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 2px rgba(16,24,40,.05)}}
.card.done{{border-left:3px solid #0a9d5c}} .card.pend{{border-left:3px solid #e08300}}
.row{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}}
.no{{font-size:12px;color:#6b7280;font-family:ui-monospace,Menlo,monospace;word-break:break-all}}
.pill{{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;white-space:nowrap}}
.pill.done{{background:#e6f7ef;color:#0a7d4b}} .pill.pend{{background:#fff5e5;color:#b46a00}}
.pill.red{{background:#feecec;color:#c62828}} .pill.blue{{background:#e8f0fe;color:#1a56db}}
.pill.gray{{background:#f1f2f4;color:#5f6672}}
.loc{{font-size:15px;font-weight:700;margin:2px 0 3px}}
.addr{{font-size:12.5px;color:#5f6672;margin-bottom:10px}}
.sec{{font-size:12px;color:#8a909c;font-weight:600;margin:12px 0 6px;padding-top:10px;border-top:1px dashed #eceef1}}
.it{{background:#fafbfc;border-radius:9px;padding:10px 11px;margin-bottom:7px}}
.it .n{{font-weight:700;font-size:13.5px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.it .d{{font-size:12px;color:#5f6672;margin-top:3px}}
.kv{{display:flex;justify-content:space-between;font-size:12.5px;padding:3px 0}}
.kv span:first-child{{color:#6b7280}} .kv span:last-child{{font-weight:600}}
.money{{display:flex;justify-content:space-between;align-items:baseline;background:#f6f9ff;border-radius:9px;padding:11px 12px;margin-top:8px}}
.money em{{font-style:normal;font-size:12px;color:#4b5563}}
.money b{{font-size:19px;color:#1f6feb;font-weight:700}}
.line{{font-size:12.5px;padding:7px 0;border-bottom:1px dashed #eceef1;display:flex;justify-content:space-between;gap:10px}}
.line:last-child{{border-bottom:0}}
.line .l{{flex:1;min-width:0}}
.line .l b{{font-weight:600;display:block;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.line .l i{{font-style:normal;color:#8a909c;font-size:11.5px}}
.line .r{{font-weight:700;color:#1a1d23;white-space:nowrap}}
.empty{{text-align:center;color:#8a909c;padding:48px 16px;font-size:13px}}
.contact{{display:flex;gap:8px;margin-top:10px}}
.contact a{{flex:1;text-align:center;text-decoration:none;padding:9px 0;border-radius:9px;font-size:13px;font-weight:600;background:#f1f5ff;color:#1f6feb}}
.ft{{text-align:center;color:#9aa0ac;font-size:11px;padding:8px 16px 0}}
/* ---- 日期筛选 ---- */
.filter{{background:#fff;border-radius:12px;padding:12px;margin-bottom:12px;box-shadow:0 1px 2px rgba(16,24,40,.05)}}
.filter .ftitle{{display:flex;align-items:center;justify-content:space-between;font-size:12.5px;font-weight:700;color:#374151;margin-bottom:10px}}
.filter .ftitle em{{font-style:normal;font-weight:600;color:#8a909c;font-size:11.5px}}
.chips{{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px;margin-bottom:10px;-webkit-overflow-scrolling:touch}}
.chips::-webkit-scrollbar{{display:none}}
.chips button{{flex:0 0 auto;border:1px solid #dcdfe4;background:#fff;color:#4b5563;padding:6px 12px;border-radius:16px;font-size:12px;font-weight:600;white-space:nowrap}}
.chips button.on{{background:#eef4ff;border-color:#1f6feb;color:#1f6feb}}
.dfield{{display:flex;align-items:center;gap:8px}}
.dfield input[type=date]{{flex:1;min-width:0;border:1px solid #dcdfe4;border-radius:9px;padding:9px 10px;font-size:13px;color:#1a1d23;background:#fff;-webkit-appearance:none;appearance:none;font-family:inherit}}
.dfield .tilde{{color:#9aa0ac;font-size:13px;flex:0 0 auto}}
.dfield .clr{{flex:0 0 auto;border:0;background:#f1f2f4;color:#5f6672;border-radius:9px;padding:9px 12px;font-size:12.5px;font-weight:600}}
.dhint{{margin-top:9px;font-size:12px;color:#6b7280;line-height:1.6}}
.dhint b{{color:#1f6feb}}
.rst{{margin-top:10px;padding-top:10px;border-top:1px dashed #eceef1;display:flex;justify-content:space-between;font-size:12.5px}}
.rst span{{color:#6b7280}} .rst b{{color:#1a1d23}}
@media (prefers-color-scheme:dark){{
 body{{background:#111418;color:#e6e8ec}}
 .stats{{background:#262b33}} .stats div{{background:#1a1e24}}
 .card{{background:#1a1e24;box-shadow:none}} .it{{background:#21262e}}
 .addr,.kv span:first-child,.it .d{{color:#9aa2ae}}
 .money{{background:#1c2530}} .line{{border-color:#2a3038}}
 .tabs button{{background:#1a1e24;border-color:#2a3038;color:#b6bdc9}}
 .ft{{color:#6b7280}}
 .filter{{background:#1a1e24;box-shadow:none}}
 .filter .ftitle{{color:#c9d0da}}
 .chips button{{background:#21262e;border-color:#2a3038;color:#b6bdc9}}
 .chips button.on{{background:#1c2f4d;border-color:#3b8bff;color:#8fb8ff}}
 .dfield input[type=date]{{background:#21262e;border-color:#2a3038;color:#e6e8ec;color-scheme:dark}}
 .dfield .clr{{background:#2a3038;color:#b6bdc9}}
 .rst{{border-color:#2a3038}} .rst b{{color:#e6e8ec}}
}}
</style>
</head>
<body>
<div class="hd">
  <div class="sub">维修申报 · 分派单查询</div>
  <h1>{unit}</h1>
  <div class="meta"><span id="upd">数据更新 {generated_at}</span><span>共 {total} 单</span></div>
</div>
<div class="stats">
  <div><b id="s-total">{total}</b><span>分派单数</span></div>
  <div class="b-done"><b id="s-done">{done}</b><span>已维修</span></div>
  <div class="b-pend"><b id="s-pend">{pending}</b><span>待维修</span></div>
  <div class="b-amt"><b id="s-amt">{amount}</b><span>累计金额</span></div>
</div>
<div class="wrap">
  <div class="filter">
    <div class="ftitle">按派单时间筛选 <em>申请单·派单时间</em></div>
    <div class="chips" id="chips">
      <button data-q="all" class="on">全部</button>
      <button data-q="7">近7天</button>
      <button data-q="30">近30天</button>
      <button data-q="tm">本月</button>
      <button data-q="lm">上月</button>
      <button data-q="ty">今年</button>
    </div>
    <div class="dfield">
      <input type="date" id="d-from" aria-label="开始日期">
      <span class="tilde">至</span>
      <input type="date" id="d-to" aria-label="结束日期">
      <button class="clr" id="d-reset">重置</button>
    </div>
    <div class="dhint" id="dhint"></div>
    <div class="rst">
      <span>筛选结果</span>
      <b id="rst-txt">—</b>
    </div>
  </div>

  <div class="tabs">
    <button class="on" data-f="all">全部 <span class="tn">{total}</span></button>
    <button data-f="pending">待维修 <span class="tn">{pending}</span></button>
    <button data-f="done">已维修 <span class="tn">{done}</span></button>
  </div>
  <div id="list">{cards}</div>
  <div id="empty" class="empty" style="display:none"><span id="empty-title">该条件下暂无单据</span><div id="empty-sub" style="margin-top:8px;font-size:12px;color:#9aa0ac"></div></div>
</div>
<div class="ft">数据来自钉钉 AI 表格「维修申报」，如与现场不符请联系总部资产管理部</div>
<script>
(function(){{
  var cards=[].slice.call(document.querySelectorAll('.card'));
  var fromEl=document.getElementById('d-from'),toEl=document.getElementById('d-to');
  var chips=[].slice.call(document.querySelectorAll('#chips button'));
  var tabs=[].slice.call(document.querySelectorAll('.tabs button'));
  var state={{q:'all',f:'all',from:'',to:''}};

  // 本单位实际派单日期范围（用于空结果时给出可选项，避免误判"页面坏了"）
  var days=cards.map(function(c){{return c.dataset.d}}).filter(Boolean).sort();
  var dmin=days[0]||'', dmax=days[days.length-1]||'';
  if(dmin){{
    fromEl.min=dmin;toEl.min=dmin;fromEl.max=dmax;toEl.max=dmax;
  }}

  function pad(n){{return n<10?'0'+n:''+n}}
  function fmt(d){{return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())}}
  function today(){{var d=new Date();d.setHours(0,0,0,0);return d}}
  function shift(n){{var d=today();d.setDate(d.getDate()+n);return d}}

  function rangeOf(q){{
    var t=today(),y=t.getFullYear(),m=t.getMonth();
    if(q==='7')  return [fmt(shift(-6)),fmt(t)];
    if(q==='30') return [fmt(shift(-29)),fmt(t)];
    if(q==='tm') return [fmt(new Date(y,m,1)),fmt(t)];
    if(q==='lm') return [fmt(new Date(y,m-1,1)),fmt(new Date(y,m,0))];
    if(q==='ty') return [fmt(new Date(y,0,1)),fmt(t)];
    return ['',''];
  }}

  function apply(){{
    var lo=state.from,hi=state.to,n=0,sum=0,p=0,d=0;
    cards.forEach(function(c){{
      var day=c.dataset.d||'';
      var okDate=(!lo||(day&&day>=lo))&&(!hi||(day&&day<=hi));
      var okTab=(state.f==='all'||c.dataset.s===state.f);
      var show=okDate&&okTab;
      c.style.display=show?'':'none';
      if(show){{n++;sum+=parseFloat(c.dataset.amt||0);
        if(c.dataset.s==='done')d++;else p++;}}
    }});
    document.getElementById('empty').style.display=n?'none':'block';

    document.getElementById('s-total').textContent=n;
    document.getElementById('s-done').textContent=d;
    document.getElementById('s-pend').textContent=p;
    document.getElementById('s-amt').textContent='¥'+sum.toLocaleString('zh-CN',
      {{minimumFractionDigits:2,maximumFractionDigits:2}});

    tabs[0].querySelector('.tn').textContent=n;
    tabs[1].querySelector('.tn').textContent=p;
    tabs[2].querySelector('.tn').textContent=d;

    var parts=[];
    if(lo||hi)parts.push((lo||'最早')+' 至 '+(hi||'今天'));
    parts.push(state.f==='pending'?'待维修':state.f==='done'?'已维修':'全部状态');
    document.getElementById('dhint').innerHTML=parts.join(' · ')+
      '　→　命中 <b>'+n+'</b> 单';

    var sub=document.getElementById('empty-sub');
    var et=document.getElementById('empty-title');
    if(n===0&&dmin){{
      var scope=(lo||hi)?'该时间段内':'';
      sub.textContent=scope+'本单位派单日期范围：'+dmin+' 至 '+dmax+
        (state.f!=='all'?'（当前只看'+(state.f==='pending'?'待维修':'已维修')+'）':'');
      if(et)et.textContent='该条件下暂无单据';
    }}else if(n===0&&!dmin){{
      // 这家单位一单都没接到过：不要说「该条件下」，会让人以为是自己筛没了
      if(et)et.textContent='暂无分派给本单位的维修单';
      sub.textContent='总部有单派过来后，本页会自动更新';
    }}else{{sub.textContent='';}}
  }}

  chips.forEach(function(b){{
    b.onclick=function(){{
      chips.forEach(function(x){{x.classList.remove('on')}});b.classList.add('on');
      state.q=b.dataset.q;
      var r=rangeOf(state.q);
      state.from=r[0];state.to=r[1];
      fromEl.value=r[0];toEl.value=r[1];
      apply();
    }};
  }});

  function manual(){{
    state.from=fromEl.value;state.to=toEl.value;
    chips.forEach(function(x){{x.classList.remove('on')}});
    apply();
  }}
  fromEl.onchange=manual; toEl.onchange=manual;

  document.getElementById('d-reset').onclick=function(){{
    state.from='';state.to='';fromEl.value='';toEl.value='';
    chips.forEach(function(x){{x.classList.remove('on')}});
    chips[0].classList.add('on');state.q='all';
    apply();
  }};

  tabs.forEach(function(b){{
    b.onclick=function(){{
      tabs.forEach(function(x){{x.classList.remove('on')}});b.classList.add('on');
      state.f=b.dataset.f;apply();
    }};
  }});

  apply();

  // ---- 实时性：后台数据一变就自动换页 ----
  // 页面本身是静态文件（秒开、可缓存），实时靠这里主动探版本。
  // rev 变了 → 整页刷新（数据真变了）；rev 没变但构建时间变了 → 只原地
  // 更新「数据更新 HH:MM」文字，不闪屏。2026-11-09 修正：只比 rev 会让
  // 供应商在数据没变时一直看到旧时间，误以为系统停更（实际已反复踩坑）。
  var MYREV=document.querySelector('meta[name=rev]').content||'';
  var UPDEL=document.getElementById('upd');
  var MYTIME=UPDEL?UPDEL.textContent.replace(/^数据更新\\s*/,''):'';
  var probing=false;
  function probe(){{
    if(probing||document.hidden)return;
    // 极老的微信内核没有 fetch：直接放弃探测，页面照常显示（只是不自动刷新）
    if(typeof fetch!=='function')return;
    probing=true;
    fetch('data/rev.json?ts='+Date.now(),{{cache:'no-store'}})
      .then(function(r){{return r.ok?r.json():null}})
      .then(function(j){{
        if(!j||!j.rev)return;
        if(j.rev===MYREV){{
          // 数据没变，只是重新构建过：把时间戳文字换新即可
          var t=j.generated_at;
          if(t&&MYTIME&&t!==MYTIME){{
            MYTIME=t;
            if(UPDEL)UPDEL.textContent='数据更新 '+t;
          }}
          return;
        }}
        // 数据变了：保留用户当前的筛选条件，只把内容换掉
        var keepF=state.f, keepQ=state.q, keepFrom=state.from, keepTo=state.to;
        try{{sessionStorage.setItem('jkwx-f',JSON.stringify(
          {{f:keepF,q:keepQ,from:keepFrom,to:keepTo}}))}}catch(e){{}}
        location.reload();
      }})
      .catch(function(){{}})
      .then(function(){{probing=false}});
  }}
  // 回到前台立刻探一次（微信里切回来最需要新数据），在页时每 45s 探一次
  document.addEventListener('visibilitychange',function(){{if(!document.hidden)probe()}});
  setInterval(probe,45000);

  // 从上次刷新恢复筛选条件
  try{{
    var saved=JSON.parse(sessionStorage.getItem('jkwx-f')||'null');
    if(saved){{
      sessionStorage.removeItem('jkwx-f');
      if(saved.q&&saved.q!=='all'){{
        var cb=chips.filter(function(x){{return x.dataset.q===saved.q}})[0];
        if(cb){{chips.forEach(function(x){{x.classList.remove('on')}});cb.classList.add('on')}}
      }}
      if(saved.from||saved.to){{
        fromEl.value=saved.from||'';toEl.value=saved.to||'';
        state.from=saved.from||'';state.to=saved.to||'';
      }}
      if(saved.f&&saved.f!=='all'){{
        var tb=tabs.filter(function(x){{return x.dataset.f===saved.f}})[0];
        if(tb){{tabs.forEach(function(x){{x.classList.remove('on')}});tb.classList.add('on')}}
        state.f=saved.f;
      }}
      apply();
    }}
  }}catch(e){{}}
}})();
</script>
</body>
</html>"""


CARD = """<div class="card {status}" data-s="{status}" data-d="{dateKey}" data-amt="{amtRaw}">
  <div class="row">
    <div class="no">{no}</div>
    <div class="pill {status}">{statusText}</div>
  </div>
  <div class="loc">{loc}</div>
  <div class="addr">{address}</div>
  <div class="kv"><span>申请人 / 电话</span><span>{applicant} {phone}</span></div>
  <div class="kv"><span>派单时间</span><span>{dispatchDate}</span></div>
  <div class="kv"><span>校区申报</span><span>{applyDate}</span></div>
  {items}
  {bills}
</div>"""


def render_cards(unit: dict) -> str:
    if not unit["orders"]:
        return ""
    out = []
    for o in unit["orders"]:
        items_html = []
        for it in o["items"]:
            label, cls = urgency_of(it["level"])
            bits = []
            if it["qty"]:
                bits.append(f'数量 {it["qty"]}')
            if it["loc"]:
                bits.append(f'位置 {it["loc"]}')
            if it["desc"]:
                bits.append(it["desc"])
            items_html.append(
                f'<div class="it"><div class="n">{it["name"] or "未命名"}'
                f'<span class="pill {cls}">{label}</span></div>'
                f'<div class="d">{" · ".join(bits) or "—"}</div></div>')

        bill_html = ""
        if o["bills"]:
            parts = []
            for b in o["bills"]:
                rows = "".join(
                    f'<div class="line"><div class="l"><b>{ln["cat"] or "维修项"}</b>'
                    f'<i>{"数量 " + (str(int(ln["qty"])) if ln["qty"] is not None else "—")}'
                    f' × 单价 {money(ln["price"])}'
                    f'{" · 人工费 " + money(ln["labor"]) if ln["labor"] else ""}'
                    f'{" · " + ln["desc"] if ln["desc"] else ""}</i></div>'
                    f'<div class="r">{money(ln["amount"])}</div></div>'
                    for ln in b["lines"]) or '<div class="line"><div class="l">未填明细</div></div>'
                extra = []
                if b["tax"]:
                    extra.append(f"税费 {money(b['tax'])}")
                if b["remote"]:
                    extra.append(f"远程费 {money(b['remote'])}")
                parts.append(
                    f'<div class="sec">维修清单 · 填报 {b["fillTime"] or "—"}'
                    f'{" · 完成 " + b["doneDate"] if b["doneDate"] else ""}'
                    f'{" · 校区已签字" if b["signed"] else ""}</div>{rows}'
                    f'<div class="money"><em>{" · ".join(extra) or "含税总额"}</em>'
                    f'<b>{money(b["total"])}</b></div>')
            bill_html = "".join(parts)
        else:
            bill_html = ('<div class="sec">维修清单</div>'
                         '<div class="it"><div class="d">该单尚未收到维修清单报单，'
                         '请完成维修后扫码填报维修服务单</div></div>')

        out.append(CARD.format(
            status=o["status"],
            statusText="已维修" if o["status"] == "done" else "待维修",
            no=o["no"] or "—",
            loc=o["area"] or "—",
            address=o["address"] or "—",
            applicant=o["applicant"] or "—",
            phone=o["phone"] or "",
            applyDate=o["applyDate"] or "—",
            dispatchDate=o["dispatchDate"] or "—",
            dateKey=o["dispatchDate"] if o["dispatchDate"] != "—" else "",
            amtRaw=f'{o["total"]:.2f}',
            items="".join(items_html),
            bills=bill_html,
        ))
    return "".join(out)


def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再原子替换。

    serve.py 会在后台刷新数据，而供应商可能正好在这时扫码。
    直接 write_text 会出现「文件已被截断但还没写完」的瞬间，
    扫码的人就拿到半截 HTML（白屏或样式全丢）。os.replace 是原子的，
    读到的要么是旧的完整版，要么是新的完整版。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_pages(ds: dict) -> list[dict]:
    """每家单位 → site/u-<token>.html。

    **文件名只用令牌，不用序号**：序号（u01/u02…）会随单量排序变化而漂移，
    同一家单位换了文件名 → 已发出去的二维码立刻失效。令牌由单位名哈希而来，
    只要单位名不变，地址就永远不变。
    """
    SITE.mkdir(parents=True, exist_ok=True)
    manifest = []
    for u in ds["units"]:
        slug = f"u-{unit_token(u['name'])}"
        st = u["stats"]
        html = PAGE.format(
            unit=u["name"],
            rev=ds["rev"],
            generated_at=ds["generated_at"],
            total=st["total"], done=st["done"], pending=st["pending"],
            amount=money(st["amount"]),
            cards=render_cards(u),
        )
        body = f"{slug}.html"
        atomic_write(SITE / body, html)
        manifest.append({"name": u["name"], "slug": slug, "stats": st})
    payload = json.dumps({"generated_at": ds["generated_at"], "rev": ds["rev"],
                          "built_at": ds["built_at"], "units": manifest},
                         ensure_ascii=False, indent=2)
    atomic_write(DATA / "units.json", payload)
    # 对外只放一个仅含 rev 的文件。⚠ 千万不要把 units.json 放进 SITE/：
    # 隧道是公网可达的，units.json 含全部 30 家单位的名称+令牌，
    # 谁下载一次就等于拿到全部钥匙（2026-11-05 发现并修复的泄露）。
    # 页面轮询只需要 rev，所以公开 rev.json 就够了。
    atomic_write(SITE / "data" / "rev.json",
                 json.dumps({"generated_at": ds["generated_at"], "rev": ds["rev"]},
                            ensure_ascii=False))
    # 兜底：清掉历史版本留在公开目录里的 units.json
    stale = SITE / "data" / "units.json"
    if stale.exists():
        stale.unlink(missing_ok=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", help="只处理指定维修单位")
    args = ap.parse_args(argv)

    for d in (SITE, DATA, QRDIR):
        d.mkdir(parents=True, exist_ok=True)

    ds = build_dataset(args.unit)
    if not ds["units"]:
        print("没有找到匹配的维修单位数据", file=sys.stderr)
        return 1
    manifest = write_pages(ds)

    # 清掉已经不在名单里的旧页面：单位改名/删除后，残留页面还能被旧二维码打开，
    # 显示的是过期数据。只删本程序自己的 u-*.html，不碰 index.html 与 assets。
    keep = {f"{m['slug']}.html" for m in manifest}
    for old in SITE.glob("u-*.html"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
            print(f"已清理失效页面 {old.name}", file=sys.stderr)

    print(json.dumps({"generated_at": ds["generated_at"], "rev": ds["rev"],
                      "units": manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
