"""Build the pristine, pre-processed state shipped with the app (seed/): reference data + ~35 back-dated runs.

Every run goes through the real pipeline (local engine, no AI calls, no pacing). Usage: python tools/make_seed.py
"""
import json, os, shutil, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATA_DIR"] = os.path.join(ROOT, "seed")
os.environ["STAGE_MIN_MS"] = "0"
sys.path.insert(0, ROOT)
shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)
from app import config, db, pipeline

EXPECT = dict(approve="APPROVE", escalate_price="ESCALATE", escalate_limit="ESCALATE", escalate_receipt="ESCALATE",
              hold_number="HOLD", reject_duplicate="REJECT", reject_unknown="REJECT", reject_blocked="REJECT", reject_statement="REJECT")

db.init_schema(); db.reset_runtime()
man = json.load(open(os.path.join(config.REF_DIR, "seed_invoices", "manifest.json")))
man.sort(key=lambda m: m["created_at"])
bad, counts = 0, {}
t0 = time.time()
for m in man:
    path = os.path.join(config.REF_DIR, "seed_invoices", m["file"])
    rid = pipeline.create_run(path, m["file"], source="seed", sample_id=m["id"], mode="local", created_at=m["created_at"], as_of=m["created_at"][:10])
    res = pipeline.process(rid, mode="local", pace_ms=0)
    got = res["decision"] if res else "ERR"
    counts[got] = counts.get(got, 0) + 1
    ok = got == EXPECT[m["scenario"]] or (res and got == "ESCALATE" and any(f["code"] == "PO_AMBIGUOUS" for f in res["findings"]))
    bad += 0 if ok else 1
    print(f"{'OK ' if ok else 'XX '} {m['id']} {m['scenario']:18} -> {got:9} {m['vendor'][:26]:26} {m['number'] or '(none)':14} ${m['total']:>10,.2f} | {(res or {}).get('headline','')[:70]}")
    if not ok and res:
        for f in res["findings"]:
            if f["severity"] in ("fail",):
                print("        ", f["code"], f["outcome"], f["detail"][:130])
print("decisions:", counts, "| mismatches:", bad, f"| {time.time()-t0:.1f}s")
c = db.conn(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.execute("PRAGMA journal_mode=DELETE"); c.commit(); db.close_all()
for f in os.listdir(config.DATA_DIR):
    if f.endswith(("-wal", "-shm")):
        os.remove(os.path.join(config.DATA_DIR, f))
