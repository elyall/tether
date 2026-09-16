import zarr
from tether import Repo

repo = Repo.find(".")
h = repo.open("zarr/imaging", read_only=False)
root = zarr.open_group(store=h.session.store, mode="a")
root["labels"][:] = 7
h.session.commit("labels: candidate A")
