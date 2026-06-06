# Health Checks

1. compare the machine time with the broadcast time, if out of sync by more than a minute, we should note that and compensate accordingly for accurate recordings.
1. periodically check the status of all enabled DVB adapters, if they are free do a tuning check. Note any failures and remove the adapter from the pool.