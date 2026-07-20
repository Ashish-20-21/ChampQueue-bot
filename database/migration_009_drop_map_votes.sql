-- Drop map_votes — confirmed dead multiple times (architecture review +
-- this session): cast_map_vote/get_map_votes in db.py have zero call
-- sites anywhere in cogs/ or services/, consistent with the locked
-- "no map vote, backend picks randomly" decision (DECISIONS.md).
-- Safe standalone drop, no other table references map_votes.

drop table if exists map_votes;
