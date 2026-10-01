-- name: RecallPlaybook :many
-- Candidates for the current (host, path, previous). The supervisor filters to those
-- whose next-shape matches exactly one element on the current page.
select * from playbooks
where host = $1 and path = $2 and previous = $3
  and seen >= 2
  and (succeeded + failed = 0 or succeeded::float / (succeeded + failed) >= 0.5)
order by last_used desc, succeeded desc
limit 8;

-- name: RecordPlaybook :exec
insert into playbooks (host, path, previous, next, seen, last_used)
values ($1, $2, $3, $4, 1, now())
on conflict (host, path, previous, next)
do update set seen = playbooks.seen + 1, last_used = now();

-- name: BumpPlaybookSuccess :exec
update playbooks
set succeeded = succeeded + 1, last_used = now()
where host = $1 and path = $2 and previous = $3 and next = $4;

-- name: BumpPlaybookFailure :exec
update playbooks
set failed = failed + 1, last_used = now()
where host = $1 and path = $2 and previous = $3 and next = $4;

-- name: ListPlaybooksByHost :many
select * from playbooks where host = $1 order by last_used desc;

-- name: ForgetPlaybook :exec
delete from playbooks where id = $1;
