-- Disposable local DB only, after mailing/returned fixtures and migrations.
create table if not exists district_users(id uuid primary key default gen_random_uuid(),district_id uuid,user_id uuid,email text,full_name text,role text);
create table if not exists comptroller_closure_reviews(id uuid primary key default gen_random_uuid(),district_id uuid references districts(id),workflow_status text default 'PENDING_REVIEW',matched_account_number text,reviewed_by uuid,created_at timestamptz default now());
alter table bpp_work_claims add column if not exists actor_email text;
alter table bpp_work_claims add column if not exists source_table text;
alter table bpp_work_claims add column if not exists item_id text;
alter table bpp_work_claims add column if not exists claimed_at timestamptz default now();
