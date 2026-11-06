#!/usr/bin/env python3
"""把 gh-pages/ 部署到 Netlify，让跳转页获得永久地址。

## ⛔ 已废弃（2026-11-06），不要再用，也不要重新启用

Netlify 免费 credit 是**按账号**算的，本方案每 10 分钟部署一次整站
= 144 次/天，几天就把额度打爆，随后报：

    Account credit usage exceeded - new deploys are blocked until credits are added

额度爆掉那一刻，**本机和云端同时**被拒，线上数据静止不动——真实事故。
另外微信把 `netlify.app` 整域风控，扫码跳过去会被拦截页挡住。

现在的发布方式是 GitHub Pages artifact 部署（见 `cloud_sync.py::write_site_dir`、
`gh-workflow-sync.yml`），Pages 的 Source 已在仓库 Settings 里切成
「GitHub Actions」，因此**推 main 不再触发 legacy 构建、不会再覆盖整站**。

本文件仅作为历史记录与应急参考保留。真要回退，先确认 Netlify credit 有余量。

## 为什么当初选 Netlify 而不是 GitHub Pages

国内实测（2026-11-05，成都，微信 UA）：
- `*.netlify.app`  0.7~1.7s，稳定
- `*.github.io`    1.7~2.5s
- `*.vercel.app`   超时 10s（排除）
- `*.pages.dev`    DNS 被污染（排除）
- `*.ngrok-free.app`  完全不通（排除）

另外 GitHub Pages 免费版**只能开公开仓库**，而公开仓库的文件列表任何人可看。
Netlify 没有这个限制，站点可以不公开列出资源。

## 认证：token 是账号级的，两个平台一样

Netlify 官方文档明确：PAT 创建时唯一的权限开关是「是否授权访问 SAML 团队」，
**没有站点级 scope**，能操作你有权限的全部站点。
所以"用 Netlify 权限更小"是错的，别拿这个当理由（2026-11-05 更正）。

选它的真实理由是：访问更快 + 不强制公开。

## 为什么一次请求部署，而不是逐文件传

GitHub 的 contents API 一个文件一次请求，32 个文件 = 32 次 PUT，
还容易踩 500 次/分钟的限流。
Netlify 的 deploy API 是「先建 deploy → 逐个上传所需文件的 sha256 →
最后一次性提交」，且**内容寻址**：内容没变的文件自动跳过。
更省事的是 Netlify 接受 `POST /sites/{id}/deploys` 带 `zip` 参数整体上传，
本脚本用后者——一个请求搞定。

## 用法

    set NETLIFY_TOKEN=nfp_xxx      (或写进 netlify_token.txt)
    python push_netlify.py         # 首次会建站点
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GH_DIR = ROOT / "gh-pages"
SITE = ROOT / "site"
TOKEN_FILE = ROOT / "netlify_token.txt"
SITE_FILE = ROOT / "netlify_site.txt"     # 存 site_id，之后复用
HOST_FILE = ROOT / "host_base.txt"        # 托管站地址（make_jump.py 的跳转目标）
API = "https://api.netlify.com/api/v1"
SITE_NAME = "jkwx-repair-portal"


def token() -> str:
    t = os.environ.get("NETLIFY_TOKEN", "").strip()
    if not t and TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return t


def call(method: str, path: str, tok: str, body: dict | None = None,
         raw: bytes | None = None, ctype: str = "application/json") -> tuple[int, dict]:
    url = path if path.startswith("http") else API + path
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("User-Agent", "jkwx-repair-portal")
    if data:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(payload) if payload.strip() else {})
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, {"message": payload[:400]}
    except Exception as e:  # noqa: BLE001
        return 0, {"message": repr(e)}


def zip_site() -> bytes:
    """打 zip：跳转页（gh-pages/）+ 单位页快照（site/）。

    Netlify 支持 `zip` 形式的部署请求，一个 POST 就能整站上线，
    不用像 GitHub 那样逐文件 PUT（32 次请求 + 限流风险）。

    ⚠ site/index.html 必须排除：那是本地隧道的首页，会和 gh-pages 的
    单页路由器（?u= 跳转入口）抢根路径。路由器必须赢，否则二维码全废。

    ⚠ 安全：只放 30 张 u-*.html（各自带独立令牌路径）+ data/rev.json。
    Netlify API 直传没有公开仓库文件列表，令牌不可枚举的设计保得住；
    但**绝不**把这些放进公开 GitHub 仓库（gh-pages 推送仍是纯路由器）。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(GH_DIR.rglob("*")):
            if p.is_file() and p.stat().st_size > 0:
                z.write(p, p.relative_to(GH_DIR).as_posix())
        for p in sorted(SITE.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(SITE).as_posix()
            if rel == "index.html":        # 路由器占有根路径，见上
                continue
            if rel.startswith("data/units"):  # 历史遗留的泄露文件，永不外发
                continue
            z.write(p, rel)
    return buf.getvalue()


def looks_like_netlify(t: str) -> bool:
    """Netlify PAT 形如 nfp_xxx；老的 64 位十六进制也接受。"""
    t = t.strip()
    return t.startswith("nfp_") or (len(t) == 64 and all(c in "0123456789abcdef" for c in t.lower()))


def main() -> int:
    tok = token()
    if not tok:
        print("缺少 Netlify token。请任选一种：\n"
              "  1) set NETLIFY_TOKEN=nfp_xxx\n"
              f"  2) 写入 {TOKEN_FILE.name}", file=sys.stderr)
        return 2
    if not looks_like_netlify(tok):
        # 只提示，不阻断：Netlify 未来可能改格式，硬拦会让人误以为是网络问题
        print(f"提示：token 形态不像常见的 nfp_ 开头（长度 {len(tok)}），仍继续尝试。")

    if not (GH_DIR / "index.html").exists():
        print("跳转页还没生成，先跑 make_jump.py", file=sys.stderr)
        return 2

    # 1) 确认身份
    st, me = call("GET", "/accounts", tok)
    if st != 200:
        print(f"token 无效（{st}）：{me.get('message')}", file=sys.stderr)
        return 1
    accts = me if isinstance(me, list) else me.get("accounts") or []
    if not accts:
        print("这个 token 下没有任何账号/团队", file=sys.stderr)
        return 1
    acct = accts[0]
    acct_slug = acct.get("slug") or acct.get("id")
    print(f"账号: {acct.get('name')} ({acct_slug})")

    # 2) 找已有站点，没有就建
    site_id = ""
    if SITE_FILE.exists():
        site_id = SITE_FILE.read_text(encoding="utf-8").strip()
    st, sites = call("GET", "/sites?per_page=100", tok)
    if st == 200 and isinstance(sites, list):
        for s in sites:
            if s.get("name") == SITE_NAME or s.get("id") == site_id:
                site_id = s["id"]
                print(f"已存在站点: {s.get('name')} ({site_id})")
                break

    if not site_id:
        st, site = call("POST", "/sites", tok, {"name": SITE_NAME})
        if st not in (200, 201):
            # 子域名可能被别人占了（Netlify 全局唯一）→ 让 Netlify 自动取名
            print(f"按名字建站失败（{st}）：{site.get('message')}，改用自动子域名")
            st, site = call("POST", "/sites", tok, {})
            if st not in (200, 201):
                print(f"建站点失败（{st}）：{site.get('message')}", file=sys.stderr)
                return 1
        site_id = site["id"]
        print(f"已创建站点: {site.get('name')} ({site_id})")

    SITE_FILE.write_text(site_id + "\n", encoding="utf-8")

    # 2.5) 关掉站点「访问保护（SSO 登录）」
    #
    # ⚠ 2026-11-06 实测踩坑：这个 Netlify 账号（新 free 计划）默认给站点开
    # `sso_login=true`，公网访问全部返回 401 + 跳 app.netlify.com/edge-access，
    # 扫码的人会被要求登录 Netlify，等于二维码全废。
    # 账号级 `site_sso_login` 改不动（API 提示 not eligible），但**站点级可以关**，
    # 关完 5 秒内生效。放这里每次部署都幂等执行，重建站点也不会再踩。
    st, _ = call("PATCH", f"/sites/{site_id}", tok, {"sso_login": False})
    if st == 200:
        print("站点访问保护已关闭（sso_login=false）")
    else:
        print(f"提示：关闭访问保护返回 {st}", file=sys.stderr)

    # 3) 整站 zip 部署（一个请求）
    zdata = zip_site()
    st, dep = call("POST", f"/sites/{site_id}/deploys", tok,
                   raw=zdata, ctype="application/zip")
    if st not in (200, 201):
        print(f"部署失败（{st}）：{dep.get('message')}", file=sys.stderr)
        return 1

    state = dep.get("state")
    url = (dep.get("ssl_url") or dep.get("url") or "").rstrip("/")
    print(f"部署已提交，状态: {state}")
    print(f"站点地址: {url}")

    st, site = call("GET", f"/sites/{site_id}", tok)
    base = (site.get("ssl_url") or site.get("url") or url).rstrip("/") if st == 200 else url
    if not base:
        print("拿不到站点地址，跳转页无法指向托管站", file=sys.stderr)
        return 1

    # 4) 记录托管站地址（make_jump.py 读它做跳转目标）
    old_host = HOST_FILE.read_text(encoding="utf-8").strip() if HOST_FILE.exists() else ""
    HOST_FILE.write_text(base + "\n", encoding="utf-8")
    if old_host != base:
        # 跳转目标变了 → 路由器重生成（BASE 要烧进 html）→ 推回 GitHub Pages
        # 注意：二维码指向 github.io 跳转页，永远不用重出码。
        r = subprocess.run([sys.executable, str(ROOT / "make_jump.py")],
                           cwd=str(ROOT), capture_output=True, timeout=120)
        if r.returncode == 0:
            import os
            gtok = os.environ.get("GITHUB_TOKEN", "").strip()
            gf = ROOT / "github_token.txt"
            if not gtok and gf.exists():
                gtok = gf.read_text(encoding="utf-8").strip()
            if gtok:
                env = dict(os.environ, GITHUB_TOKEN=gtok)
                r2 = subprocess.run([sys.executable, str(ROOT / "push_jump.py")],
                                    cwd=str(ROOT), capture_output=True,
                                    timeout=300, env=env)
                if r2.returncode == 0:
                    print("跳转页路由器已更新并推送到 GitHub Pages")
                else:
                    out = (r2.stderr or r2.stdout or b"").decode("utf-8", "replace")
                    print(f"推送 GitHub 跳转页失败: {out[:300]}", file=sys.stderr)
            else:
                print("跳转页路由器已在本地重生成（无 GitHub token，未推送）")
        else:
            out = (r.stderr or r.stdout or b"").decode("utf-8", "replace")
            print(f"make_jump 重生成失败: {out[:300]}", file=sys.stderr)

    print(f"\n托管站地址（单位页快照，电脑关机也能开）:\n  {base}/u-df02912d014a.html")
    print(f"二维码入口（不变）: 参考 jump_base.txt / base_url.txt")
    print("\n首次部署约 10~30 秒生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
