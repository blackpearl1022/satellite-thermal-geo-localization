#!/bin/bash
eval "$(conda shell.bash hook)"
conda activate VPR_SSL

# bing + foxtech
python h5_transformer.py --database_name satellite --database_index 0 --queries_name foxtechmapping --queries_index 0 --compress --sample_num 5000

# bing + google
python h5_transformer.py --database_name satellite --database_index 0 --queries_name satellite --queries_index 1 --compress --sample_num 20000

# bing + bing
python h5_transformer.py --database_name satellite --database_index 0 --queries_name satellite --queries_index 0 --compress --sample_num 20000


