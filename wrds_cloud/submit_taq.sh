#!/bin/bash
# ---------------------------------------------------------------------------
# Submit a TAQ pull as an SGE array job, one task per calendar year.
#
#   Usage:    qsub -t 2017-2024 submit_taq.sh
#   (or for a single year)   qsub -t 2024-2024 submit_taq.sh
#
# Each array task processes one year of dates from the event panel.
# Tasks are independent: safe to run all years in parallel.
# ---------------------------------------------------------------------------
#$ -N taq_pull
#$ -cwd
#$ -j y                     # merge stderr into stdout
#$ -o $HOME/taq_logs/$JOB_NAME.$JOB_ID.$TASK_ID.log
#$ -q all.q                 # default queue on WRDS Cloud
#$ -l h_rt=24:00:00         # 24h walltime cap; raise if needed
#$ -l m_mem_free=16G        # request 16 GB RAM per task

# Make sure the log directory exists (qsub fails silently if it doesn't).
mkdir -p $HOME/taq_logs

# WRDS Cloud Python module with pandas and wrds.
# If your account has conda, replace this with: source activate myenv
module load python/3.11.4

# ----- paths -----
SCRATCH_DIR=/scratch/eur/$USER/taq_batch
PANEL=$SCRATCH_DIR/ticker_day_panel.csv
OUT=$SCRATCH_DIR/bars_1min
LOGS=$SCRATCH_DIR/logs
SCRIPT_DIR=$SCRATCH_DIR/code

YEAR=$SGE_TASK_ID

echo "===================================================="
echo "Job   : $JOB_ID  task=$SGE_TASK_ID  year=$YEAR"
echo "Host  : $(hostname)"
echo "Start : $(date -Iseconds)"
echo "===================================================="

cd "$SCRIPT_DIR"

python -u bulk_taq_pull.py \
    --panel   "$PANEL" \
    --out     "$OUT" \
    --logs    "$LOGS" \
    --year    "$YEAR" \
    --workers 3 \
    --ticker-batch 400

echo "End   : $(date -Iseconds)"
