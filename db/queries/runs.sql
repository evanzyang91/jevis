-- name: CreateRun :one
insert into runs (goal, refined_goal, start_url, text_model, vision_model)
values ($1, $2, $3, $4, $5)
returning *;

-- name: GetRun :one
select * from runs where id = $1;

-- name: ListRuns :many
select * from runs order by started_at desc limit $1;

-- name: SetRunStatus :exec
update runs
set status = $2,
    ended_at = case when $2 in ('done','blocked','error') then now() else ended_at end
where id = $1;

-- name: AddRunCost :exec
update runs set usd_total = usd_total + $2 where id = $1;
