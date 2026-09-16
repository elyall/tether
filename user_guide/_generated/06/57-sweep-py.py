import sys

import lance
import pyarrow as pa
import zarr
from tether import Repo

trial = int(sys.argv[sys.argv.index("--trial") + 1])
repo = Repo.find(".")
h = repo.open("zarr/imaging", read_only=False)
root = zarr.open_group(store=h.session.store, mode="a")
root["labels"][:] = 10 + trial
h.session.commit(f"labels: sweep trial {trial}")

s = repo.open("scratch/embeddings", read_only=False)
rows = pa.table({"cell_id": [1, 2, 3], "embedding_0": [trial / 10.0] * 3})
lance.write_dataset(rows, s.dataset, mode="append")
