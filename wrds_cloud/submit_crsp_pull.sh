#!/bin/bash
#$ -N crsp_pull_v2
#$ -cwd
#$ -j y
#$ -o /scratch/eur/$USER/taq_batch/logs/crsp_pull_v2.log
#$ -l h_rt=02:00:00
#$ -l m_mem_free=8G

# Pull CRSP DSF for common stock (US + foreign-incorporated) on NYSE/AMEX/NASDAQ.

source /etc/profile.d/modules.sh
module load python/3.14

python /scratch/eur/$USER/taq_batch/code/pull_crsp_dsf.py \
    --start 2018-10-01 \
    --end   2024-12-31 \
    --out   /scratch/eur/$USER/crsp_v2
