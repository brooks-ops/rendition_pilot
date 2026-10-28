-- Structured browser captures are originals, never mislabeled as PDFs.
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values('county-clerk-records','county-clerk-records',false,26214400,array['application/json'])
on conflict(id) do nothing;
