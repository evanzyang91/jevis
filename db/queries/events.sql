-- name: AppendEvent :exec
insert into events (run_id, seq, kind, payload)
values ($1, $2, $3, $4);

-- name: ListEventsSince :many
select * from events
where run_id = $1 and seq > $2
order by seq asc
limit $3;

-- name: ListEventsByKind :many
select * from events
where run_id = $1 and kind = $2
order by seq asc;

-- name: LatestSeq :one
select coalesce(max(seq), 0)::int as seq from events where run_id = $1;
