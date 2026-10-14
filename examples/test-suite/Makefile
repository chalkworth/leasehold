# Six independent jobs. Run in parallel, they show how many ran at once (job.sh).
JOBS := t1 t2 t3 t4 t5 t6

all: $(JOBS)

$(JOBS):
	@./job.sh $@

.PHONY: all $(JOBS)
