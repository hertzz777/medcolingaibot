CREATE TABLE entitlements (
    user_id BIGINT,
    product TEXT,
    charge_id TEXT UNIQUE,
    granted_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, product)
);

CREATE TABLE daily_usage (
    user_id BIGINT,
    usage_date DATE,
    count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, usage_date)
);
