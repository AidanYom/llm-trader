# Production's settings, committed so a deploy can never quietly change them (HANDOFF §16). The alarm email
# isn't here: it goes in the gitignored local.auto.tfvars (copy local.auto.tfvars.example).

# dry_run: scheduled runs call Claude and record orders but send none. submit: they send paper orders.
run_mode = "submit"
