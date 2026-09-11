import lance
import pyarrow as pa
from tether import Repo

repo = Repo.find(".")
f = repo.open("features", read_only=False)  # on the trunk: Lance's own main
areas = pa.table({"cell_id": [1, 2, 3], "area": [112.0, 96.0, 128.5]})
lance.write_dataset(areas, f.dataset, mode="append")
