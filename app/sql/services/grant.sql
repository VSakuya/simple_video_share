SELECT g.*, s.slug, s.upstream, s.entry_path FROM service_grants g
JOIN custom_services s ON s.id=g.service_id
JOIN users u ON u.id=g.user_id
WHERE g.token_hash=? AND g.kind=? AND g.expires_at>? AND s.enabled=1
AND u.must_change_password=0 AND u.password_hash=g.password_stamp;
