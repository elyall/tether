import lance
import pyarrow as pa

qc = pa.table({"cell_id": [4, 5], "area": [101.0, 99.5]})
lance.write_dataset(qc, "~/data/features.lance", mode="append")
