#!/usr/bin/env python3
"""Publishes due posts from schedule/<date>.json (written by the Mac publisher when you tap Schedule).

Each file: {"date","version","at":"<ISO time with offset>","caption","urls":[...],"status":"pending"}
status: pending -> publishing -> published | failed.  (cancelled/expired are set by the Mac / this script.)
Only the standard library. The Instagram token comes from the IG_TOKEN secret and is never printed.
"""
import datetime as dt, glob, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

IG = "https://graph.instagram.com"
STALE_HOURS = 36  # never publish something that was due more than this long ago


def ig(method, path, params):
    params = dict(params, access_token=os.environ["IG_TOKEN"])
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{IG}/{path}?{data.decode()}") if method == "GET" else urllib.request.Request(f"{IG}/{path}", data)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Instagram {path}: {e.read().decode()[:300].replace(os.environ['IG_TOKEN'], '***')}")


def wait_ready(cid, tries=30):
    for _ in range(tries):
        st = ig("GET", cid, {"fields": "status_code"}).get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {cid} status {st}")
        time.sleep(5)
    raise RuntimeError(f"container {cid} not ready after {tries * 5}s")


def publish(job):
    me = ig("GET", "me", {"fields": "user_id"})["user_id"]
    children = []
    for u in job["urls"]:
        if isinstance(u, str):  # older schedule files: plain image URLs
            u = {"url": u, "type": "IMAGE"}
        cid = None
        if u["type"] == "VIDEO":
            try:
                cid = ig("POST", f"{me}/media", {"media_type": "VIDEO", "video_url": u["url"], "is_carousel_item": "true"})["id"]
                wait_ready(cid, 90)
            except RuntimeError as e:
                print("video refused, using the still image:", str(e)[:120])
                cid = None
                u = {"url": u.get("fallback") or u["url"].rsplit(".", 1)[0] + ".jpg", "type": "IMAGE"}
        if cid is None:
            cid = ig("POST", f"{me}/media", {"image_url": u["url"], "is_carousel_item": "true"})["id"]
            wait_ready(cid)
        children.append(cid)
    parent = ig("POST", f"{me}/media", {"media_type": "CAROUSEL", "children": ",".join(children), "caption": job["caption"]})["id"]
    wait_ready(parent)
    media_id = ig("POST", f"{me}/media_publish", {"creation_id": parent})["id"]
    return ig("GET", media_id, {"fields": "permalink"}).get("permalink", media_id)


def save(path, job, msg):
    json.dump(job, open(path, "w"), ensure_ascii=False, indent=1)
    if os.environ.get("NO_GIT"):
        return
    subprocess.run(["git", "config", "user.name", "scheduled-publish"], check=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], check=True)
    subprocess.run(["git", "add", path], check=True)
    subprocess.run(["git", "commit", "-qm", msg], check=True)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase"], check=False)
    raise RuntimeError("could not push the schedule file")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    for path in sorted(glob.glob("schedule/*.json")):
        job = json.load(open(path))
        if job.get("status") != "pending":
            continue
        at = dt.datetime.fromisoformat(job["at"])
        if at > now:
            continue
        if now - at > dt.timedelta(hours=STALE_HOURS):
            job["status"] = "expired"
            save(path, job, f"expired {job['date']}")
            continue
        job["status"] = "publishing"
        save(path, job, f"publishing {job['date']}")  # lock: the Mac won't touch it now
        try:
            job["link"] = publish(job)
            job["status"] = "published"
        except Exception as e:
            job["status"], job["error"] = "failed", str(e)[:400]
        job["finished"] = now.isoformat()
        save(path, job, f"{job['status']} {job['date']}")
        print(job["date"], job["status"])
    print("done")


if __name__ == "__main__":
    main()
