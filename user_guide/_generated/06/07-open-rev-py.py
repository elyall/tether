from tether import Repo

repo = Repo.find(".")
h = repo.open("zarr/imaging", rev="695fe286e11b")   # read-only, at the pinned snapshot
