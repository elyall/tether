import zarr
from tether import Repo

repo = Repo.find(".")
h = repo.open("scratch/probe")
root = zarr.create_group(store=h.session.store)
root.create_array("embeddings", shape=(3, 4), dtype="f4")[:] = 0.5
h.session.commit("embeddings: probe")
