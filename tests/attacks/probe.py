"""Attack a running Shipshape portal over HTTP, the way an outsider would.

    python tests/attacks/probe.py .dogfood.toml

Standard library only, like the organizers' checker, and it reads the same
.dogfood.toml (base URL and the demo sessions). It changes state: it asks
for voting links, fails sign-ins on purpose (locking the demo organizer's
password sign-in for 15 minutes) and would cast ballots if the portal let
it. Point it at a throwaway container, never a live event. The ids (DG1,
SC2, ...) are the attacks in JUDGING.md, section 11. Each line ends with
what happened: STOPPED when the portal refused as it should, OPEN when the
attack got through.
"""

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request

SLUG = "sample-hack-2026"
results = []


def request(url, method="GET", body=None, headers=None, form=None):
    req = urllib.request.Request(url, method=method)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if form is not None:
        req.data = form.encode()
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def cookie(header):
    name, _, value = (header or "").partition(":")
    return {name.strip(): value.strip()} if value else {}


def report(attack, stopped, detail):
    results.append(stopped)
    print(f"{attack:58} {'STOPPED' if stopped else 'OPEN':8} {detail}")


def main(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    base = cfg["portal"]["base_url"].rstrip("/")
    auth = cfg.get("auth", {})
    participant, judge, judge_b = (cookie(auth.get(k)) for k in ("participant", "judge_a", "judge_b"))

    # Deadline gaming
    status, _ = request(f"{base}/api/events/{SLUG}/submission", "POST", {"title": "late", "summary": "late"},
                        participant)
    report("DG1 submit after the deadline (old API)", status == 403, f"{status}")
    status, _ = request(f"{base}/api/v1/events/{SLUG}/submission", "POST", {"tagline": "changed after close"},
                        participant)
    report("DG4 edit a submitted project after the deadline (REST)", status == 403, f"{status}")
    status, _ = request(f"{base}/api/v1/events/{SLUG}/submission/withdraw", "POST", {}, participant)
    report("DG8 withdraw after the deadline", status == 403, f"{status}")

    # Judge collusion
    # judge_a is fixture judge jdg_24; judge_b asks for judge_a's scores, and judge_a for another judge's.
    status, _ = request(f"{base}/api/judge/scores?judge=jdg_24", headers=judge_b)
    status_b, _ = request(f"{base}/api/judge/scores?judge=jdg_01", headers=judge)
    report("JC1 a judge asks for a peer's scores", status == 403 and status_b == 403, f"{status}, {status_b}")
    status, _ = request(f"{base}/api/v1/events/{SLUG}/judging/results", headers=judge)
    report("JC1 a judge asks for the rankings", status in (403, 404), f"{status}")

    # Submission scraping
    _, missing = request(f"{base}/api/v1/projects/987654")
    hidden = public = 0
    identical = True
    for pk in range(1, 80):
        status, body = request(f"{base}/api/v1/projects/{pk}")
        if status == 200:
            public += 1
        else:
            hidden += 1
            identical &= body == missing
    report("SC2 walk project ids looking for hidden ones", identical,
           f"{public} public, {hidden} not: every other id answers exactly like a missing one")
    emails = set()
    status, body = request(f"{base}/api/v1/projects?limit=100")
    emails |= set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", body))
    for page in ("/projects/", f"/events/{SLUG}/", f"/embed/events/{SLUG}/gallery.json"):
        emails |= set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", request(base + page)[1]))
    report("SC4 harvest participants' emails from public pages", not emails,
           f"{len(emails)} addresses found" + (f": {sorted(emails)[:3]}" if emails else ""))
    status, _ = request(f"{base}/api/v1/events/{SLUG}/export/archive.zip", headers=participant)
    report("SC8 download the whole event as a participant", status in (403, 404), f"{status}")

    # Community vote: sybils and stuffing
    statuses = [request(f"{base}/api/v1/events/{SLUG}/ballot/email", "POST", {"email": f"mallory+{n}@example.org"})[0]
                for n in range(4)]
    report("SY2 one inbox asks for links as mallory+0..+3@", statuses[-1] == 429, f"{statuses}")
    sent = 3
    for n in range(40):
        status, _ = request(f"{base}/api/v1/events/{SLUG}/ballot/email", "POST", {"email": f"sock{n}@example.net"},
                            {"X-Forwarded-For": f"203.0.113.{n}"})
        if status == 429:
            break
        sent += 1
    report("SY6 fake networks with X-Forwarded-For to keep asking", status == 429,
           f"refused after {sent} links from one real network, whatever the header said")
    status, body = request(f"{base}/api/events/{SLUG}/vote/results")
    status_p, _ = request(f"{base}/api/events/{SLUG}/vote/results", headers=participant)
    report("BS6 read the running count while voting is open", status == 403 and status_p == 403,
           f"{status} {json.loads(body).get('error', '') if body.startswith('{') else ''}, participant {status_p}")
    status, _ = request(f"{base}/api/v1/events/{SLUG}/ballot", "POST", {"votes": {"1": 1}},
                        {**participant, "Origin": "https://evil.example"})
    status_f, _ = request(f"{base}/api/v1/events/{SLUG}/ballot", "POST", form="votes=1",
                          headers=participant)
    report("BS7 cast a ballot from another site in a voter's browser", status == 403 and status_f == 415,
           f"cross-origin JSON {status}, form post {status_f}")
    status, _ = request(f"{base}/api/v1/events/{SLUG}/ballot/email/confirm", "POST", {"token": "guessed-token"})
    report("BS4 confirm a voting link without the email", status in (400, 403, 404), f"{status}")

    # Accounts, since every identity rule rests on them
    codes = [request(f"{base}/api/v1/auth/tokens", "POST",
                     {"name": "probe", "email": "organizer@example.org", "password": f"wrong-{n}"})[0]
             for n in range(7)]
    report("A1 guess the organizer's password", codes[-1] == 429, f"{codes}")

    print(f"\n{sum(results)} of {len(results)} attacks stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ".dogfood.toml"))
