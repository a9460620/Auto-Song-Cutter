from __future__ import annotations

import json
from http.cookiejar import MozillaCookieJar
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def default_cookie_candidates() -> list[Path]:
    return [
        SCRIPT_DIR / "cookies.json",
        SCRIPT_DIR / "cookies.txt",
        Path.cwd() / "cookies.json",
        Path.cwd() / "cookies.txt",
        Path.home() / "cookies.json",
        Path.home() / "cookies.txt",
        Path.home() / "Documents" / "歌回" / "cookies.json",
        Path.home() / "Documents" / "歌回" / "cookies.txt",
    ]


def load_cookie(cookie_text: str | None = None, cookie_file: Path | str | None = None) -> tuple[str, Path | None]:
    if cookie_text:
        return cookie_text.strip(), None

    candidates = [Path(cookie_file).expanduser()] if cookie_file else default_cookie_candidates()
    for candidate in candidates:
        if not candidate.is_file():
            continue

        raw = candidate.read_text(encoding="utf-8", errors="ignore").strip()
        if not raw:
            continue

        if "=" in raw and "\t" not in raw and "\n" not in raw:
            return raw, candidate.resolve()

        if raw.startswith("{") or raw.startswith("["):
            cookie = load_json_cookie(raw)
            if cookie:
                return cookie, candidate.resolve()

        try:
            jar = MozillaCookieJar(str(candidate))
            jar.load(ignore_discard=True, ignore_expires=True)
            cookie = "; ".join(f"{item.name}={item.value}" for item in jar)
            if cookie:
                return cookie, candidate.resolve()
        except Exception:
            continue

    return "", None


def load_json_cookie(raw: str) -> str:
    data = json.loads(raw)

    if isinstance(data, dict) and isinstance(data.get("cookie_info"), dict):
        cookies = data["cookie_info"].get("cookies", [])
    elif isinstance(data, dict) and isinstance(data.get("cookies"), list):
        cookies = data["cookies"]
    elif isinstance(data, list):
        cookies = data
    else:
        cookies = []

    pairs = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            pairs.append(f"{name}={value}")

    return "; ".join(pairs)


def require_bilibili_login(cookie_text: str | None = None, cookie_file: Path | str | None = None) -> tuple[str, Path | None]:
    cookie, resolved_file = load_cookie(cookie_text, cookie_file)
    if cookie:
        return cookie, resolved_file

    candidates = [Path(cookie_file).expanduser()] if cookie_file else default_cookie_candidates()
    checked = "\n".join(f"  - {path}" for path in candidates)
    raise FileNotFoundError(
        "尚未找到 Bilibili 登入 Cookie，因此不能使用 bilibili_tools。\n"
        "請先登入並提供 cookies.json / cookies.txt，或用 --cookie / --cookie-file 指定。\n"
        f"已檢查：\n{checked}"
    )
