"""Initial frozen database schema."""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE catalog_records (
	id VARCHAR(36) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	source_id TEXT NOT NULL,
	identity_key TEXT NOT NULL,
	upstream_uuid VARCHAR(36),
	parent_id VARCHAR(36),
	source_baseline_json TEXT NOT NULL,
	typed_fields_json TEXT NOT NULL,
	extension_json TEXT DEFAULT '{}' NOT NULL,
	raw_json TEXT NOT NULL,
	source_updated_at VARCHAR(32),
	first_seen_at VARCHAR(32) NOT NULL,
	last_seen_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (kind, identity_key),
	UNIQUE (upstream_uuid),
	CHECK (kind IN
	('brand','material','filament','variant','size','store','purchase_link')),
	CHECK (json_valid(source_baseline_json) AND json_type(source_baseline_json) =
	'object'),
	CHECK (json_valid(typed_fields_json) AND json_type(typed_fields_json) = 'object'),
	CHECK (json_valid(extension_json) AND json_type(extension_json) = 'object'),
	CHECK (json_valid(raw_json) AND json_type(raw_json) = 'object'),
	FOREIGN KEY(parent_id) REFERENCES catalog_records (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE catalog_runs (
	id VARCHAR(36) NOT NULL,
	"trigger" VARCHAR(9) NOT NULL,
	schedule_key TEXT,
	started_at VARCHAR(32) NOT NULL,
	finished_at VARCHAR(32),
	status VARCHAR(11) NOT NULL,
	attempt_count INTEGER NOT NULL,
	source_version TEXT,
	source_digest BLOB,
	error_code VARCHAR(128),
	PRIMARY KEY (id),
	CHECK (trigger IN ('scheduled','manual')),
	CHECK (status IN ('running','succeeded','failed','interrupted')),
	CHECK (attempt_count >= 1),
	CHECK (source_digest IS NULL OR length(source_digest) = 32),
	UNIQUE (schedule_key)
)

""")
    op.execute(
        """CREATE UNIQUE INDEX uq_catalog_runs_running
        ON catalog_runs (status) WHERE status = 'running'"""
    )
    op.execute("""
CREATE TABLE catalog_schedule_state (
	id INTEGER NOT NULL,
	last_handled_slot_key TEXT,
	timezone_name TEXT NOT NULL,
	wall_time VARCHAR(5) DEFAULT '03:00' NOT NULL,
	PRIMARY KEY (id),
	CHECK (id = 1),
	CHECK (wall_time = '03:00')
)

""")
    op.execute("""
CREATE TABLE catalog_schemas (
	schema_name TEXT NOT NULL,
	source_json TEXT NOT NULL,
	source_version TEXT NOT NULL,
	fetched_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (schema_name)
)

""")
    op.execute("""
CREATE TABLE inventory_settings (
	id INTEGER NOT NULL,
	low_stock_numerator INTEGER DEFAULT 1 NOT NULL,
	low_stock_denominator INTEGER DEFAULT 10 NOT NULL,
	PRIMARY KEY (id),
	CHECK (id = 1),
	CHECK (low_stock_numerator > 0),
	CHECK (low_stock_denominator > 0 AND low_stock_denominator <= 1000000),
	CHECK (low_stock_numerator <= low_stock_denominator)
)

""")
    op.execute("""
CREATE TABLE inventory_state (
	id INTEGER NOT NULL,
	dataset_generation VARCHAR(36) NOT NULL,
	PRIMARY KEY (id),
	CHECK (id = 1),
	UNIQUE (dataset_generation)
)

""")
    op.execute("""
CREATE TABLE locations (
	id VARCHAR(36) NOT NULL,
	name TEXT NOT NULL,
	revision INTEGER DEFAULT 1 NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	updated_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CHECK (revision >= 1)
)

""")
    op.execute("""
CREATE TABLE restore_control (
	id INTEGER NOT NULL,
	mode VARCHAR(8) DEFAULT 'normal' NOT NULL,
	PRIMARY KEY (id),
	CHECK (id = 1),
	CHECK (mode IN ('normal','restore'))
)

""")
    op.execute("""
CREATE TABLE token_records (
	id VARCHAR(36) NOT NULL,
	name TEXT COLLATE "BINARY" NOT NULL,
	token_hash BLOB NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	revoked_at VARCHAR(32),
	PRIMARY KEY (id),
	CHECK (length(token_hash) = 32),
	UNIQUE (name),
	UNIQUE (token_hash)
)

""")
    op.execute("""
CREATE TABLE catalog_record_links (
	id VARCHAR(36) NOT NULL,
	source_record_id VARCHAR(36) NOT NULL,
	relationship_name TEXT NOT NULL,
	ordinal INTEGER NOT NULL,
	target_record_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_record_id, relationship_name, ordinal),
	CHECK (ordinal >= 0),
	FOREIGN KEY(source_record_id) REFERENCES catalog_records (id) ON DELETE RESTRICT,
	FOREIGN KEY(target_record_id) REFERENCES catalog_records (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE idempotency_records (
	token_id VARCHAR(36) NOT NULL,
	"key" TEXT NOT NULL,
	fingerprint BLOB NOT NULL,
	response_status INTEGER NOT NULL,
	response_body TEXT NOT NULL,
	replay_headers_json TEXT NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	expires_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (token_id, "key"),
	CHECK (length(key) BETWEEN 1 AND 128 AND key NOT GLOB '*[^!-~]*'),
	CHECK (length(fingerprint) = 32),
	FOREIGN KEY(token_id) REFERENCES token_records (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE local_manufacturers (
	id VARCHAR(36) NOT NULL,
	source_brand_id VARCHAR(36),
	name TEXT NOT NULL,
	notes TEXT DEFAULT '' NOT NULL,
	revision INTEGER DEFAULT 1 NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	updated_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CHECK (length(name) BETWEEN 1 AND 100),
	CHECK (trim(name) = name),
	CHECK (revision >= 1),
	FOREIGN KEY(source_brand_id) REFERENCES catalog_records (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE token_scopes (
	token_id VARCHAR(36) NOT NULL,
	scope TEXT NOT NULL,
	PRIMARY KEY (token_id, scope),
	FOREIGN KEY(token_id) REFERENCES token_records (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE local_filaments (
	id VARCHAR(36) NOT NULL,
	manufacturer_id VARCHAR(36) NOT NULL,
	source_filament_id VARCHAR(36),
	selected_variant_id VARCHAR(36),
	name TEXT NOT NULL,
	presentation_json TEXT DEFAULT '{}' NOT NULL,
	notes TEXT DEFAULT '' NOT NULL,
	default_tare_mg INTEGER,
	nominal_weight_mg INTEGER,
	revision INTEGER DEFAULT 1 NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	updated_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CHECK (length(name) BETWEEN 1 AND 120),
	CHECK (trim(name) = name),
	CHECK (revision >= 1),
	CHECK (default_tare_mg IS NULL OR (typeof(default_tare_mg) = 'integer' AND
	default_tare_mg >= 0)),
	CHECK (nominal_weight_mg IS NULL OR (typeof(nominal_weight_mg) = 'integer' AND
	nominal_weight_mg >= 0)),
	CHECK (json_valid(presentation_json) AND json_type(presentation_json) = 'object'),
	FOREIGN KEY(manufacturer_id) REFERENCES local_manufacturers (id) ON DELETE RESTRICT,
	FOREIGN KEY(source_filament_id) REFERENCES catalog_records (id) ON DELETE RESTRICT,
	FOREIGN KEY(selected_variant_id) REFERENCES catalog_records (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE assets (
	id VARCHAR(36) NOT NULL,
	source_record_id VARCHAR(36),
	local_manufacturer_id VARCHAR(36),
	local_filament_id VARCHAR(36),
	sha256 BLOB NOT NULL,
	media_type TEXT NOT NULL,
	source_metadata_json TEXT NOT NULL,
	fetched_at VARCHAR(32) NOT NULL,
	blob BLOB NOT NULL,
	PRIMARY KEY (id),
	CHECK (length(sha256) = 32),
	CHECK ((source_record_id IS NOT NULL) + (local_manufacturer_id IS NOT NULL) +
	(local_filament_id IS NOT NULL) = 1),
	FOREIGN KEY(source_record_id) REFERENCES catalog_records (id) ON DELETE RESTRICT,
	FOREIGN KEY(local_manufacturer_id) REFERENCES local_manufacturers (id) ON DELETE
	RESTRICT,
	FOREIGN KEY(local_filament_id) REFERENCES local_filaments (id) ON DELETE RESTRICT
)

""")
    op.execute(
        """CREATE UNIQUE INDEX uq_assets_source_record
        ON assets (source_record_id) WHERE source_record_id IS NOT NULL"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_assets_local_manufacturer
        ON assets (local_manufacturer_id) WHERE local_manufacturer_id IS NOT NULL"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_assets_local_filament
        ON assets (local_filament_id) WHERE local_filament_id IS NOT NULL"""
    )
    op.execute("""
CREATE TABLE source_field_overrides (
	id VARCHAR(36) NOT NULL,
	local_filament_id VARCHAR(36) NOT NULL,
	json_pointer TEXT NOT NULL,
	value_json TEXT NOT NULL,
	created_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_filament_id, json_pointer),
	CHECK (json_valid(value_json)),
	FOREIGN KEY(local_filament_id) REFERENCES local_filaments (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE spools (
	id VARCHAR(36) NOT NULL,
	filament_id VARCHAR(36) NOT NULL,
	location_id VARCHAR(36),
	tare_mg INTEGER,
	initial_mg INTEGER NOT NULL,
	remaining_mg INTEGER NOT NULL,
	price_eur_cents INTEGER,
	notes TEXT DEFAULT '' NOT NULL,
	revision INTEGER DEFAULT 1 NOT NULL,
	archived_at VARCHAR(32),
	created_at VARCHAR(32) NOT NULL,
	updated_at VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CHECK (tare_mg IS NULL OR (typeof(tare_mg) = 'integer' AND tare_mg >= 0)),
	CHECK (typeof(initial_mg) = 'integer' AND initial_mg > 0),
	CHECK (typeof(remaining_mg) = 'integer' AND remaining_mg >= 0),
	CHECK (price_eur_cents IS NULL OR (typeof(price_eur_cents) = 'integer' AND
	price_eur_cents >= 0)),
	CHECK (revision >= 1),
	FOREIGN KEY(filament_id) REFERENCES local_filaments (id) ON DELETE RESTRICT,
	FOREIGN KEY(location_id) REFERENCES locations (id) ON DELETE RESTRICT
)

""")
    op.execute("""
CREATE TABLE movements (
	id VARCHAR(36) NOT NULL,
	spool_id VARCHAR(36) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	occurred_at VARCHAR(32) NOT NULL,
	amount_mg INTEGER,
	reason TEXT,
	details_json TEXT DEFAULT '{}' NOT NULL,
	before_json TEXT,
	after_json TEXT NOT NULL,
	PRIMARY KEY (id),
	CHECK (kind IN
	('created','edited','archived','restored','consumed','weighed','measured','relocated')),
	CHECK (amount_mg IS NULL OR amount_mg > 0),
	CHECK ((kind = 'created' AND before_json IS NULL AND after_json IS NOT NULL) OR
	(kind <> 'created' AND before_json IS NOT NULL AND after_json IS NOT NULL)),
		CHECK ((kind = 'consumed' AND amount_mg IS NOT NULL AND reason IS NOT NULL AND
		length(trim(reason)) BETWEEN 1 AND 500) OR
		(kind <> 'consumed' AND amount_mg IS NULL)),
	CHECK (amount_mg IS NULL OR (typeof(amount_mg) = 'integer' AND amount_mg > 0)),
	FOREIGN KEY(spool_id) REFERENCES spools (id) ON DELETE RESTRICT
)

""")
    op.execute(
        """CREATE TRIGGER movements_no_update
        BEFORE UPDATE ON movements
        WHEN (SELECT mode FROM restore_control WHERE id=1) != 'restore'
        BEGIN SELECT RAISE(ABORT, 'movement_append_only'); END"""
    )
    op.execute(
        """CREATE TRIGGER movements_no_delete
        BEFORE DELETE ON movements
        WHEN (SELECT mode FROM restore_control WHERE id=1) != 'restore'
        BEGIN SELECT RAISE(ABORT, 'movement_append_only'); END"""
    )


def downgrade() -> None:
    op.execute("""DROP TRIGGER IF EXISTS movements_no_delete""")
    op.execute("""DROP TRIGGER IF EXISTS movements_no_update""")
    op.drop_table("movements")
    op.drop_table("spools")
    op.drop_table("source_field_overrides")
    op.drop_table("assets")
    op.drop_table("local_filaments")
    op.drop_table("token_scopes")
    op.drop_table("local_manufacturers")
    op.drop_table("idempotency_records")
    op.drop_table("catalog_record_links")
    op.drop_table("token_records")
    op.drop_table("restore_control")
    op.drop_table("locations")
    op.drop_table("inventory_state")
    op.drop_table("inventory_settings")
    op.drop_table("catalog_schemas")
    op.drop_table("catalog_schedule_state")
    op.drop_table("catalog_runs")
    op.drop_table("catalog_records")
