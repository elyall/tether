import lance
import pyarrow as pa
import zarr
from tether import Repo

repo = Repo.find(".")
h = repo.open("zarr/imaging", read_only=False)  # creates the Icechunk branch
root = zarr.open_group(store=h.session.store, mode="a")
root["labels"][:] = 3  # model v3's labels
h.session.commit("labels from model v3")

f = repo.open("features", read_only=False)  # and the Lance branch
areas = pa.table({"cell_id": [1, 2, 3], "area": [112.0, 96.0, 128.5]})
lance.write_dataset(areas, f.dataset, mode="append")
