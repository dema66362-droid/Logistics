-- Authoritative empty 2.8 schema exported before the 2.9 migration.
BEGIN TRANSACTION;
CREATE TABLE approvals (
	id INTEGER NOT NULL, 
	shipper_id INTEGER NOT NULL, 
	carrier_id INTEGER NOT NULL, 
	active BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (shipper_id, carrier_id), 
	FOREIGN KEY(shipper_id) REFERENCES organizations (id), 
	FOREIGN KEY(carrier_id) REFERENCES organizations (id)
);
CREATE TABLE audit (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	actor_id INTEGER NOT NULL, 
	shipment_id INTEGER NOT NULL, 
	action VARCHAR(60) NOT NULL, 
	details TEXT NOT NULL, 
	at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(shipment_id) REFERENCES shipments (id)
);
CREATE TABLE drivers (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	user_id INTEGER, 
	name VARCHAR(100) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE TABLE import_batches (
	"key" VARCHAR(64) NOT NULL, 
	org_id INTEGER NOT NULL, 
	order_refs TEXT NOT NULL, 
	PRIMARY KEY ("key"), 
	FOREIGN KEY(org_id) REFERENCES organizations (id)
);
CREATE TABLE login_throttles (
	"key" VARCHAR(64) NOT NULL, 
	failures INTEGER NOT NULL, 
	expires DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);
CREATE TABLE notifications (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	shipment_id INTEGER, 
	kind VARCHAR(40) NOT NULL, 
	title VARCHAR(160) NOT NULL, 
	message VARCHAR(300) NOT NULL, 
	read BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(shipment_id) REFERENCES shipments (id)
);
CREATE TABLE offers (
	id INTEGER NOT NULL, 
	shipment_id INTEGER NOT NULL, 
	carrier_id INTEGER NOT NULL, 
	driver_id INTEGER NOT NULL, 
	vehicle_id INTEGER NOT NULL, 
	price INTEGER NOT NULL, 
	pickup_date DATE NOT NULL, 
	pickup_time VARCHAR(5) NOT NULL, 
	end_date DATE NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	terms_accepted BOOLEAN NOT NULL, 
	accepted_terms_version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (price > 0), 
	FOREIGN KEY(shipment_id) REFERENCES shipments (id), 
	FOREIGN KEY(carrier_id) REFERENCES organizations (id), 
	FOREIGN KEY(driver_id) REFERENCES drivers (id), 
	FOREIGN KEY(vehicle_id) REFERENCES vehicles (id)
);
CREATE TABLE organizations (
	id INTEGER NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE participant_invitations (
	id INTEGER NOT NULL, 
	sender_user_id INTEGER NOT NULL, 
	sender_org_id INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	target_email VARCHAR(180) NOT NULL, 
	target_role VARCHAR(20) NOT NULL, 
	target_user_id INTEGER, 
	token_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	responded_at DATETIME, 
	expires DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(sender_user_id) REFERENCES users (id), 
	FOREIGN KEY(sender_org_id) REFERENCES organizations (id), 
	FOREIGN KEY(target_user_id) REFERENCES users (id), 
	UNIQUE (token_hash)
);
CREATE TABLE schema_migrations (
	version VARCHAR(20) NOT NULL, 
	applied_at DATETIME NOT NULL, 
	PRIMARY KEY (version)
);
CREATE TABLE shipments (
	id INTEGER NOT NULL, 
	ref VARCHAR(30) NOT NULL, 
	shipper_id INTEGER NOT NULL, 
	from_city VARCHAR(120) NOT NULL, 
	to_city VARCHAR(120) NOT NULL, 
	recipient VARCHAR(140) NOT NULL, 
	product VARCHAR(180) NOT NULL, 
	tons NUMERIC(7, 2) NOT NULL, 
	pickup_date DATE NOT NULL, 
	pickup_time VARCHAR(5) NOT NULL, 
	unload_date DATE NOT NULL, 
	vehicle_kind VARCHAR(30) NOT NULL, 
	mode VARCHAR(30) NOT NULL, 
	budget INTEGER NOT NULL, 
	rate INTEGER NOT NULL, 
	manager VARCHAR(100) NOT NULL, 
	epd VARCHAR(80) NOT NULL, 
	etrn VARCHAR(80) NOT NULL, 
	note TEXT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	hazardous BOOLEAN NOT NULL, 
	selected_offer_id INTEGER, 
	pickup_address VARCHAR(300) NOT NULL, 
	delivery_address VARCHAR(300) NOT NULL, 
	recipient_contact VARCHAR(200) NOT NULL, 
	terms TEXT NOT NULL, 
	terms_version INTEGER NOT NULL, 
	public_external BOOLEAN NOT NULL, 
	search_launched BOOLEAN NOT NULL, 
	tariff_source VARCHAR(30) NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (tons > 0), 
	UNIQUE (ref), 
	FOREIGN KEY(shipper_id) REFERENCES organizations (id)
);
CREATE TABLE users (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	email VARCHAR(180) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	password_hash VARCHAR(200) NOT NULL, 
	active BOOLEAN NOT NULL, 
	is_admin BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id), 
	UNIQUE (email)
);
CREATE TABLE vehicles (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	label VARCHAR(100) NOT NULL, 
	kind VARCHAR(30) NOT NULL, 
	capacity NUMERIC(7, 2) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id)
);
CREATE TABLE web_sessions (
	token_hash VARCHAR(64) NOT NULL, 
	user_id INTEGER NOT NULL, 
	csrf VARCHAR(80) NOT NULL, 
	expires DATETIME NOT NULL, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_login_throttles_expires ON login_throttles (expires);
CREATE INDEX ix_notifications_user_id ON notifications (user_id);
COMMIT;
