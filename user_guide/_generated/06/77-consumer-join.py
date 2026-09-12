import os

import psycopg

with psycopg.connect(os.environ["REGISTRY_DSN"]) as conn:
    conn.execute("create table runs (run_id text, dataset_commit text)")
    conn.execute(
        "insert into runs values ('train-2026-09-14', %s)",
        ("8e78d3cb811a673475d671980c31aa3830b3c176",),
    )
    rows = conn.execute(
        """
        select r.run_id, o.key, o.pin_id, o.state_json
        from   runs r
        join   tether.objects o on o.commit_id = r.dataset_commit
        where  r.run_id = 'train-2026-09-14'
        order  by o.key
        """
    ).fetchall()
for run_id, key, pin_id, state in rows:
    print(run_id, key, pin_id, state)
