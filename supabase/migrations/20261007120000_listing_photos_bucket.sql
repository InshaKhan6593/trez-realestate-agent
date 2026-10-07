-- The private bucket for listing photos. Locally supabase/config.toml creates
-- it; a hosted project only gets what migrations create, so it is made here
-- too (a no-op when it already exists). Private: photos are uploaded to Meta
-- and sent by id, never by public URL.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('listing-photos', 'listing-photos', false, 10485760,
        array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do nothing;
