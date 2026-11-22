"""Drives the real app against the mock Gemini service and checks the AI path, fallbacks and guards."""
import json, os, subprocess, sys, time, requests, shutil, signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODE = os.path.join(ROOT, "tests", "mock_mode.json")
APP, MOCK = "http://localhost:8771", "http://localhost:8799"
fails = []


def setmode(m):
    json.dump({"mode": m}, open(MODE, "w"))


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def wait(rid, t=60):
    for _ in range(int(t / 0.2)):
        r = requests.get(f"{APP}/api/runs/{rid}").json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.2)
    raise SystemExit("timeout")


def run(sample, mode="auto", reset=True):
    if reset:   # each scenario starts from the seeded state, otherwise a repeat of S01 is (correctly) a duplicate
        requests.post(f"{APP}/api/admin/reset")
    rid = requests.post(f"{APP}/api/runs/sample", json={"sample_id": sample, "mode": mode, "pace": "instant"}).json()["run_id"]
    return wait(rid)


def start(cmd, env, port):
    p = subprocess.Popen(cmd + ["--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=open(f"/tmp/ai_{port}.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            requests.get(f"http://localhost:{port}/", timeout=1); return p
        except Exception:
            time.sleep(0.3)
    raise SystemExit("server did not start")


setmode("ok")
mock = start([sys.executable, "-I", "-m", "uvicorn", "tests.mock_gemini:app"], dict(os.environ, PYTHONPATH=ROOT), 8799)
data = "/tmp/clr_ai"; shutil.rmtree(data, ignore_errors=True)
env = dict(os.environ, DATA_DIR=data, GEMINI_API_KEY="test-key", GEMINI_BASE=MOCK + "/v1beta", STAGE_MIN_MS="0", PYTHONPATH=ROOT)
app = start([sys.executable, "-I", "-m", "uvicorn", "app.main:app"], env, 8771)
try:
    s = requests.get(f"{APP}/api/system?probe=1").json()
    check("system reports AI configured and ping ok", s["ai"]["configured"] and s["ai"]["probe"]["ok"], str(s["ai"]["probe"]))

    r = run("S01")
    res = r["result"]
    check("S01 via AI: approved", r["decision"] == "APPROVE", r["headline"])
    check("S01 via AI: engine is the model, not local", res["engine"].startswith("gemini-"), res["engine"])
    ex = [e for e in r["events"] if e["stage"] == "extract" and e["status"] in ("done", "warn", "fail")][0]["payload"]["details"]
    boxed = sum(1 for f in ex["fields"] if f["boxes"])
    check("AI values are located on the page (evidence boxes)", boxed >= 8, f"{boxed} fields boxed")
    check("cross-check table present and all agree", ex["crosscheck"] and all(x["agree"] for x in ex["crosscheck"]))
    check("no ai_error", not res["ai_error"])

    setmode("first_429")
    r = run("S01"); res = r["result"]
    check("first model 429 -> falls through to the next model", res["engine"] == "gemini-3.6-flash" and r["decision"] == "APPROVE", res["engine"])
    prog = [e["payload"].get("message", "") for e in r["events"] if e["status"] == "progress"]
    check("fallback is narrated in the live log", any("rate-limited" in m for m in prog), "; ".join(prog)[:120])

    setmode("all_429")
    r = run("S01"); res = r["result"]
    check("all models 429 -> local reader takes over, still APPROVE", r["decision"] == "APPROVE" and res["engine"].startswith("local"), res["engine"])
    check("ai_error recorded", "rate_limited" in (res["ai_error"] or ""), res["ai_error"])

    setmode("401")
    r = run("S01"); res = r["result"]
    check("bad key -> clear auth error, local fallback", "auth" in (res["ai_error"] or "") and r["decision"] == "APPROVE", res["ai_error"])

    setmode("garbage")
    r = run("S01"); res = r["result"]
    check("non-JSON answer -> falls back, no crash", r["status"] == "done" and r["decision"] == "APPROVE", str(res["ai_error"]))

    setmode("perturb_total")
    r = run("S01"); res = r["result"]
    codes = [f["code"] for f in res["findings"] if f["severity"] != "pass"]
    check("AI misreads total -> readers disagree, not silently approved", r["decision"] != "APPROVE", f"{r['decision']} {codes}")

    setmode("obey_injection")
    r = run("X5"); res = r["result"]
    check("model 'obeys' hidden instruction -> app still escalates", r["decision"] in ("ESCALATE", "REJECT"), f"{r['decision']} {res['headline']}")
    check("hidden text finding still raised", any(f["code"] in ("HIDDEN_TEXT", "INJECTION_TEXT") for f in res["findings"]))

    setmode("ok")
    r = run("E4"); res = r["result"]
    check("E4 via AI: bank change still escalates to Treasury", r["decision"] == "ESCALATE" and res["route"] == "treasury", res["headline"])
    r = run("E1a"); res = r["result"]
    check("E1a (scan) via AI path: HOLD", r["decision"] == "HOLD", res["headline"])
    art = [a for a in res["artifacts"] if a["kind"] == "vendor_email"]
    out = [e for e in r["events"] if e["stage"] == "output" and e["status"] in ("done", "warn", "fail")][0]["payload"]["details"]
    check("vendor e-mail polished by AI and numbers intact", out["ai_polished"] and art and "$3,675.00" in art[0]["body"], str(out["ai_polished"]))

    setmode("polish_bad")
    r = run("E1a"); res = r["result"]
    out = [e for e in r["events"] if e["stage"] == "output" and e["status"] in ("done", "warn", "fail")][0]["payload"]["details"]
    art = [a for a in res["artifacts"] if a["kind"] == "vendor_email"]
    check("polish that changes a number is rejected (original kept)", not out["ai_polished"] and art and "$3,675.00" in art[0]["body"])

    setmode("ok")
    r = run("S01", mode="local"); res = r["result"]
    check("mode=local never calls the AI", res["engine"].startswith("local") and res["ai_error"] is None)
    log = requests.get(MOCK + "/_log").json()
    check("every call carried the key header", all(x["key"] == "test-key" for x in log), f"{len(log)} calls")
finally:
    for p in (app, mock):
        p.send_signal(signal.SIGTERM)
print("\nFAILED: %d" % len(fails), fails)
sys.exit(1 if fails else 0)
