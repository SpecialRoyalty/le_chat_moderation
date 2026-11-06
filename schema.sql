CREATE TABLE IF NOT EXISTS config(key text primary key, value text not null);
CREATE TABLE IF NOT EXISTS members(user_id bigint primary key, display_name text, username text, joined_at timestamptz, left_at timestamptz, messages bigint not null default 0, strikes int not null default 0, minor_strikes int not null default 0, removed_flag boolean not null default false, referred_by bigint);
CREATE TABLE IF NOT EXISTS events(id bigserial primary key, user_id bigint, kind text not null, created_at timestamptz not null default now());
CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind, created_at);
CREATE TABLE IF NOT EXISTS messages(chat_id bigint not null, message_id bigint not null, user_id bigint not null, created_at timestamptz not null default now(), media_id text, phash text, primary key(chat_id,message_id));
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id,created_at);
CREATE TABLE IF NOT EXISTS banned_names(pattern text primary key);
CREATE TABLE IF NOT EXISTS banned_words(pattern text primary key);
CREATE TABLE IF NOT EXISTS banned_media(media_id text primary key, phash text);
CREATE TABLE IF NOT EXISTS trusted(user_id bigint primary key);
CREATE TABLE IF NOT EXISTS schedules(id bigserial primary key, start_hm text not null, end_hm text not null, enabled boolean not null default true);
CREATE TABLE IF NOT EXISTS posts(id bigserial primary key, kind text not null, body text not null, photo_file_id text, button_text text, button_url text, interval_minutes int not null, enabled boolean not null default true, last_message_id bigint, last_sent_at timestamptz);
CREATE TABLE IF NOT EXISTS invite_links(invite_link text primary key, owner_id bigint not null, created_at timestamptz default now());
CREATE TABLE IF NOT EXISTS referrals(user_id bigint primary key, referrer_id bigint not null, joined_at timestamptz not null default now(), credited boolean not null default false);
CREATE INDEX IF NOT EXISTS referrals_pending ON referrals(credited,joined_at);
CREATE TABLE IF NOT EXISTS vip(id bigserial primary key, chat_id bigint not null, title text not null, description text not null, price_stars int not null, days int not null default 30, enabled boolean not null default true);
CREATE TABLE IF NOT EXISTS subscriptions(user_id bigint not null, vip_id bigint not null references vip(id), expires_at timestamptz not null, charge_id text not null, primary key(user_id,vip_id));
CREATE TABLE IF NOT EXISTS mod_reports(message_id bigint not null, reporter_id bigint not null, target_user_id bigint not null, primary key(message_id,reporter_id));

-- Native Telegram VIP multi-selection, one Stars checkout per order.
CREATE TABLE IF NOT EXISTS vip_cart(
    user_id bigint NOT NULL,
    vip_id bigint NOT NULL REFERENCES vip(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id,vip_id)
);
CREATE TABLE IF NOT EXISTS vip_preferences(
    user_id bigint PRIMARY KEY,
    lang text NOT NULL DEFAULT 'fr' CHECK (lang IN ('fr','en'))
);
CREATE TABLE IF NOT EXISTS checkout_orders(
    id text PRIMARY KEY,
    user_id bigint NOT NULL,
    total_stars bigint NOT NULL CHECK(total_stars > 0),
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')),
    created_at timestamptz NOT NULL DEFAULT now(),
    paid_at timestamptz,
    charge_id text UNIQUE
);
CREATE TABLE IF NOT EXISTS checkout_order_items(
    order_id text NOT NULL REFERENCES checkout_orders(id),
    vip_id bigint NOT NULL,
    title text NOT NULL,
    chat_id bigint NOT NULL,
    price_stars bigint NOT NULL,
    days int NOT NULL,
    subscribed_at timestamptz,
    delivered_at timestamptz,
    invite_url text,
    PRIMARY KEY(order_id,vip_id)
);
CREATE INDEX IF NOT EXISTS checkout_user_created ON checkout_orders(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS checkout_delivery ON checkout_order_items(order_id) WHERE delivered_at IS NULL;
-- A non-credited referral must remain continuously in the group to earn a point.
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS referrals_referrer_status ON referrals(referrer_id,credited,active);
-- Null means lifetime access; otherwise the subscription is time-limited.
ALTER TABLE subscriptions ALTER COLUMN expires_at DROP NOT NULL;

ALTER TABLE checkout_order_items ADD COLUMN IF NOT EXISTS invite_created_at timestamptz;

-- V3 manual order workflow (requires a separate payment verification by the operator).
-- Legacy Stars prices are deliberately NOT reinterpreted as euro prices.
ALTER TABLE vip ADD COLUMN IF NOT EXISTS price_eur numeric(10,2);
CREATE TABLE IF NOT EXISTS manual_vip_orders (
    id text PRIMARY KEY,
    user_id bigint NOT NULL,
    base_total_eur numeric(10,2) NOT NULL CHECK (base_total_eur > 0),
    final_total_eur numeric(10,2) NOT NULL CHECK (final_total_eur > 0),
    method text NOT NULL CHECK(method IN ('crypto','paypal','revolut','paysafecard','apple_pay')),
    status text NOT NULL DEFAULT 'awaiting_proof'
        CHECK(status IN ('awaiting_proof','review','approved','rejected','cancelled')),
    proof_file_id text,
    proof_message_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    proof_at timestamptz,
    reviewed_at timestamptz,
    reviewed_by bigint
);
-- Migration V8: preserve historical purchases with former methods for audit,
-- while only crypto/paypal/revolut are allowed for NEW orders at application level.
ALTER TABLE manual_vip_orders DROP CONSTRAINT IF EXISTS manual_vip_orders_method_check;
ALTER TABLE manual_vip_orders ADD CONSTRAINT manual_vip_orders_method_check
    CHECK (method IN ('crypto','paypal','revolut','paysafecard','apple_pay'));

CREATE TABLE IF NOT EXISTS manual_vip_items (
    order_id text NOT NULL REFERENCES manual_vip_orders(id),
    vip_id bigint NOT NULL,
    title text NOT NULL,
    chat_id bigint NOT NULL,
    days int NOT NULL,
    price_eur numeric(10,2) NOT NULL,
    subscribed_at timestamptz,
    invite_url text,
    invite_created_at timestamptz,
    delivered_at timestamptz,
    PRIMARY KEY(order_id,vip_id)
);
CREATE INDEX IF NOT EXISTS manual_order_pending ON manual_vip_orders(status, created_at);
CREATE INDEX IF NOT EXISTS manual_order_user ON manual_vip_orders(user_id, created_at DESC);

-- V6: media analysis (non-destructive incremental migration from V5)
ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_group_id text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sha256 text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS video_phash text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS scan_status text NOT NULL DEFAULT 'pending';
CREATE INDEX IF NOT EXISTS messages_media_user ON messages(user_id,media_id) WHERE media_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_albums ON messages(chat_id,media_group_id) WHERE media_group_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS media_catalog (
 unique_id text primary key,
 sha256 text,
 photo_phash text,
 video_phash text,
 media_type text NOT NULL,
 user_id bigint,
 file_size bigint,
 duration int,
 width int,
 height int,
 scan_status text NOT NULL DEFAULT 'pending',
 first_seen timestamptz NOT NULL DEFAULT now(),
 last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_catalog_sha ON media_catalog(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS media_catalog_photo ON media_catalog(photo_phash) WHERE photo_phash IS NOT NULL;
CREATE INDEX IF NOT EXISTS media_catalog_video ON media_catalog(video_phash) WHERE video_phash IS NOT NULL;
CREATE INDEX IF NOT EXISTS media_catalog_last_seen ON media_catalog(last_seen);
CREATE TABLE IF NOT EXISTS media_ban_keys (
 key text primary key,
 reason text NOT NULL DEFAULT 'manual',
 banned_by bigint,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS media_photo_bans (
 phash text primary key,
 banned_by bigint,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS media_photo_buckets (
 bucket text NOT NULL,
 phash text NOT NULL REFERENCES media_photo_bans(phash) ON DELETE CASCADE,
 primary key(bucket,phash)
);
CREATE TABLE IF NOT EXISTS media_video_bans (
 fingerprint text primary key,
 duration int,
 width int,
 height int,
 banned_by bigint,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_video_bans_duration ON media_video_bans(duration);
CREATE TABLE IF NOT EXISTS member_recent_joins (
 user_id bigint primary key,
 observed_join_at timestamptz NOT NULL DEFAULT now()
);

-- V6 trusted-action audit (rolling retention) and maintenance state
CREATE TABLE IF NOT EXISTS trusted_actions (
 id bigserial PRIMARY KEY,
 chat_id bigint NOT NULL,
 actor_user_id bigint NOT NULL,
 command text NOT NULL,
 target_user_id bigint,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trusted_actions_recent ON trusted_actions(created_at DESC);

-- V7: invitation-goal VIP, individual join requests, configurable raffles.
ALTER TABLE vip ADD COLUMN IF NOT EXISTS invite_goal int NOT NULL DEFAULT 0 CHECK (invite_goal BETWEEN 0 AND 100000);
ALTER TABLE vip ADD COLUMN IF NOT EXISTS always_open boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS vip_registered_chats (
  chat_id bigint PRIMARY KEY,
  registered_by bigint NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vip_invite_claims (
  user_id bigint NOT NULL,
  vip_id bigint NOT NULL REFERENCES vip(id) ON DELETE CASCADE,
  credited_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,vip_id)
);
CREATE TABLE IF NOT EXISTS vip_access_links (
  invite_link text PRIMARY KEY,
  user_id bigint NOT NULL,
  vip_id bigint NOT NULL REFERENCES vip(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS vip_access_links_user ON vip_access_links(user_id,vip_id,created_at DESC);
CREATE TABLE IF NOT EXISTS raffle_draws (
  id bigserial PRIMARY KEY,
  drawn_at timestamptz NOT NULL DEFAULT now(),
  drawn_by bigint NOT NULL,
  winner_id bigint NOT NULL,
  validated_invites int NOT NULL,
  pool_size int NOT NULL
);
CREATE INDEX IF NOT EXISTS raffle_draws_at ON raffle_draws(drawn_at DESC);
CREATE TABLE IF NOT EXISTS vip_expiry_notices (
 user_id bigint NOT NULL,
 vip_id bigint NOT NULL,
 expires_at timestamptz NOT NULL,
 sent_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(user_id,vip_id,expires_at)
);
