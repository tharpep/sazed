-- Learning v1 (My-AI plans/14): jobs.failure_class (added in 017) now holds a closed class from
-- app/learning/classes.py. The second-hit rule counts a class's jobs since its last guard item.
CREATE INDEX IF NOT EXISTS jobs_failure_class_idx ON jobs (failure_class, finished_at)
    WHERE failure_class IS NOT NULL;
