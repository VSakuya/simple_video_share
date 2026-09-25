UPDATE live_rooms SET url = '/live/' || code || '.flv' WHERE url IS NULL AND code IS NOT NULL
