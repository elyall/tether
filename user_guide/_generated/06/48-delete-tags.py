import icechunk as ic
import lance

lance.dataset("~/data/features.lance").tags.delete("tether.0a1b2c3d.e04adf84a168acd1")
storage = ic.local_filesystem_storage("~/data/imaging.icechunk")
ic.Repository.open(storage).delete_tag("tether.0a1b2c3d.4f2ff54c238bea8f")
