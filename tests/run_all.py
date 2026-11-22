"""Run every sample through the pipeline in the intended order on a fresh DB and compare with the expected decision."""
import os, sys, json, shutil, time
os.environ.setdefault("DATA_DIR", "/tmp/clr_test"); os.environ["STAGE_MIN_MS"] = "0"
os.environ.setdefault("AS_OF_DATE", "2026-11-19")
sys.path.insert(0, "/home/claude/clearance")
shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
from app import config, db, pipeline
db.init_schema(); db.reset_runtime()
man = json.load(open(os.path.join(config.SAMPLES_DIR, "manifest.json")))
order = ["S01","S02","S03","E1a","E1b","E2a","E2b","E3","E4","X1","X2","X3","X4","X5"]
byid = {m["id"]: m for m in man}
bad = 0
for sid in order:
    m = byid[sid]
    t = time.time()
    rid = pipeline.create_run(os.path.join(config.SAMPLES_DIR, m["file"]), m["file"], source="sample", sample_id=sid, mode="local")
    res = pipeline.process(rid, mode="local", pace_ms=0)
    ok = res and res["decision"] == m["expect"]
    bad += 0 if ok else 1
    print(f"{'OK ' if ok else 'XX '} {sid:4} expect={m['expect']:9} got={res['decision'] if res else 'ERR':9} risk={res['risk'] if res else '-':>3} {time.time()-t:4.1f}s | {res['headline'] if res else ''}")
    if not ok or "-v" in sys.argv:
        for f in (res or {}).get("findings", []):
            if f["severity"] in ("fail","warn") or "-vv" in sys.argv:
                print(f"       {f['severity']:5} {f['code']:22} {f['outcome'] or '':9} {f['detail'][:140]}")
print("FAILED:", bad)
