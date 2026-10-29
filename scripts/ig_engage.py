#!/usr/bin/env python3
"""Comment-keyword delivery from GitHub Actions (no Mac needed).

For every published post in schedule/*.json (last 7 days) that has "cta": {"kw","dm","reply"}, read its comments and send
each commenter who wrote the keyword ONE private reply (Instagram allows one per comment, within 7 days), plus a short public reply.
Mode lives in engage/config.json: {"mode": "dry"|"on"|"off"}; default dry (logs what it would send, sends nothing).
State: engage/state.json {seen, done_users, sent_today, day}; log: engage/log.json (last 200 events).
Only the standard library. The token comes from the IG_TOKEN secret and is never printed.
"""
import datetime as dt, glob, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

IG = "https://graph.instagram.com"
MAX_PER_DAY = 60
WEEK = 7 * 86400
TOKEN = os.environ.get("IG_TOKEN", "")


def _err(e):
    return e.read().decode()[:300].replace(TOKEN, "***")


def ig_get(path, params):
    q = urllib.parse.urlencode(dict(params, access_token=TOKEN))
    try:
        with urllib.request.urlopen(f"{IG}/{path}?{q}", timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Instagram {path}: {_err(e)}")


def ig_post(path, body):
    """JSON body, token in the header so it never appears in a URL."""
    req = urllib.request.Request(f"{IG}/{path}", json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Instagram {path}: {_err(e)}")


def kw_match(text, kw):
    kw = (kw or "").strip(" 「」\"“”")
    return bool(kw) and re.search(rf"(?<![\w]){re.escape(kw)}(?![\w])", text or "", re.I) is not None


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save_all(files, msg):
    if os.environ.get("NO_GIT"):
        return
    subprocess.run(["git", "config", "user.name", "scheduled-engage"], check=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], check=True)
    subprocess.run(["git", "add", *files], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-qm", msg], check=True)
    for _ in range(4):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase", "--autostash"], check=False)
    raise RuntimeError("could not push engage state")


def main():
    os.makedirs("engage", exist_ok=True)
    cfg = load("engage/config.json", {"mode": "dry"})
    mode = cfg.get("mode", "dry")
    if mode == "off":
        print("engage off")
        return
    st = load("engage/state.json", {})
    seen, done = st.setdefault("seen", {}), st.setdefault("done_users", {})
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st.get("day") != today:
        st["day"], st["sent_today"] = today, 0
    log = load("engage/log.json", [])
    now = time.time()
    me = ig_get("me", {"fields": "user_id,username"})
    my_id, my_name = str(me.get("user_id")), me.get("username", "")
    media = {m["permalink"].rstrip("/"): m["id"] for m in ig_get("me/media", {"fields": "id,permalink", "limit": 30}).get("data", [])}
    for path in sorted(glob.glob("schedule/*.json")):
        job = load(path, {})
        cta, link = job.get("cta"), (job.get("link") or "").rstrip("/")
        if job.get("status") != "published" or not (cta and cta.get("kw") and cta.get("dm")) or link not in media:
            continue
        try:
            when = dt.datetime.fromisoformat(job["finished"]).timestamp()
        except Exception:
            when = now
        if now - when > WEEK:
            continue
        media_id = media[link]
        try:
            comments = ig_get(f"{media_id}/comments", {"fields": "id,text,username,from", "limit": 50}).get("data", [])
        except RuntimeError as e:
            log.append({"t": now, "post": job["date"], "event": "comments_failed", "error": str(e)[:200]})
            continue
        for c in reversed(comments):  # oldest first
            if c["id"] in seen:
                continue
            seen[c["id"]] = now
            user = c.get("username", "")
            if user == my_name or str((c.get("from") or {}).get("id", "")) == my_id or not kw_match(c.get("text"), cta["kw"]):
                continue
            key = f"{media_id}:{user}"
            if key in done:
                continue
            ev = {"t": now, "post": job["date"], "user": user, "comment": (c.get("text") or "")[:60]}
            if st["sent_today"] >= MAX_PER_DAY:
                log.append(dict(ev, event="daily_limit"))
                continue
            if mode != "on":
                log.append(dict(ev, event="dry_would_send", chars=len(cta["dm"])))
                done[key] = "dry"
                continue
            try:
                ig_post(f"{my_id}/messages", {"recipient": {"comment_id": c["id"]}, "message": {"text": cta["dm"][:1000]}})
                st["sent_today"] += 1
                done[key] = now
                log.append(dict(ev, event="sent"))
                if cta.get("reply"):
                    try:
                        ig_post(f"{c['id']}/replies", {"message": cta["reply"][:300]})
                    except RuntimeError as e:
                        log.append(dict(ev, event="public_reply_failed", error=str(e)[:160]))
            except RuntimeError as e:
                done[key] = "failed"
                log.append(dict(ev, event="send_failed", error=str(e)[:300]))
    for k in [k for k, t in seen.items() if now - t > WEEK]:
        del seen[k]
    json.dump(st, open("engage/state.json", "w"), indent=1)
    json.dump(log[-200:], open("engage/log.json", "w"), ensure_ascii=False, indent=1)
    if not os.path.exists("engage/config.json"):
        json.dump(cfg, open("engage/config.json", "w"))
    save_all(["engage"], f"engage {mode}")
    print("engage", mode, "ok")


if __name__ == "__main__":
    main()
