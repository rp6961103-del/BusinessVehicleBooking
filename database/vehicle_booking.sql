CREATE DATABASE IF NOT EXISTS vehicle_booking
	CHARACTER SET utf8mb4
	COLLATE utf8mb4_unicode_ci;

USE vehicle_booking;

CREATE TABLE IF NOT EXISTS users (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	name VARCHAR(100) NOT NULL,
	phone VARCHAR(20) NOT NULL,
	email VARCHAR(255) NULL,
	password_hash VARCHAR(255) NULL,
	is_active BOOLEAN NOT NULL DEFAULT TRUE,
	PRIMARY KEY (id),
	UNIQUE KEY uq_users_phone (phone)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS owners (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	owner_name VARCHAR(100) NOT NULL,
	phone VARCHAR(20) NOT NULL,
	email VARCHAR(255) NOT NULL,
	password_hash VARCHAR(255) NULL,
	is_active BOOLEAN NOT NULL DEFAULT TRUE,
	PRIMARY KEY (id),
	UNIQUE KEY uq_owners_email (email),
	KEY idx_owners_phone (phone)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS password_reset_tokens (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	account_type VARCHAR(10) NOT NULL,
	account_id INT UNSIGNED NOT NULL,
	token_hash CHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	used_at DATETIME NULL,
	PRIMARY KEY (id),
	UNIQUE KEY uq_password_reset_token_hash (token_hash),
	KEY idx_password_reset_account (account_type, account_id, expires_at),
	KEY idx_password_reset_expiry (expires_at)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS password_reset_attempts (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	account_type VARCHAR(10) NOT NULL,
	email_hash CHAR(64) NOT NULL,
	ip_hash CHAR(64) NOT NULL,
	requested_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	KEY idx_password_reset_attempt_ip (ip_hash, requested_at),
	KEY idx_password_reset_attempt_email (account_type, email_hash, requested_at)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS admins (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	username VARCHAR(100) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	is_active BOOLEAN NOT NULL DEFAULT TRUE,
	created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
	PRIMARY KEY (id),
	UNIQUE KEY uq_admins_username (username)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS app_settings (
	setting_key VARCHAR(100) NOT NULL,
	setting_value TEXT NOT NULL,
	updated_by INT UNSIGNED NULL,
	updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
	PRIMARY KEY (setting_key),
	KEY idx_app_settings_updated_by (updated_by),
	CONSTRAINT fk_app_settings_admin
		FOREIGN KEY (updated_by) REFERENCES admins (id)
		ON UPDATE CASCADE
		ON DELETE SET NULL
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS subscription_requests (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	request_id CHAR(36) NOT NULL,
	owner_id INT UNSIGNED NOT NULL,
	plan_code VARCHAR(20) NOT NULL,
	amount DECIMAL(8, 2) NOT NULL,
	duration_months SMALLINT UNSIGNED NOT NULL,
	status VARCHAR(30) NOT NULL DEFAULT 'pending_verification',
	payment_reference VARCHAR(100) NULL,
	requested_at DATETIME NOT NULL,
	starts_at DATETIME NULL,
	expires_at DATETIME NULL,
	verified_by INT UNSIGNED NULL,
	verified_at DATETIME NULL,
	verification_reason VARCHAR(500) NULL,
	PRIMARY KEY (id),
	UNIQUE KEY uq_subscription_requests_request_id (request_id),
	UNIQUE KEY uq_subscription_requests_payment_reference (payment_reference),
	KEY idx_subscription_requests_owner_status (owner_id, status, expires_at),
	KEY idx_subscription_requests_status_requested (status, requested_at),
	CONSTRAINT fk_subscription_requests_owner
		FOREIGN KEY (owner_id) REFERENCES owners (id)
		ON UPDATE CASCADE
		ON DELETE RESTRICT,
	CONSTRAINT fk_subscription_requests_admin
		FOREIGN KEY (verified_by) REFERENCES admins (id)
		ON UPDATE CASCADE
		ON DELETE SET NULL
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS vehicles (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	vehicle_name VARCHAR(100) NOT NULL,
	vehicle_type VARCHAR(100) NOT NULL,
	owner_id INT UNSIGNED NOT NULL,
	contact_number VARCHAR(20) NOT NULL,
	location VARCHAR(255) NOT NULL,
	is_active BOOLEAN NOT NULL DEFAULT TRUE,
	PRIMARY KEY (id),
	KEY idx_vehicles_owner_id (owner_id),
	CONSTRAINT fk_vehicles_owner
		FOREIGN KEY (owner_id) REFERENCES owners (id)
		ON UPDATE CASCADE
		ON DELETE RESTRICT
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS vehicle_routes (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	vehicle_id INT UNSIGNED NOT NULL,
	from_location VARCHAR(255) NOT NULL,
	to_location VARCHAR(255) NOT NULL,
	rent DECIMAL(10, 2) NOT NULL,
	PRIMARY KEY (id),
	KEY idx_vehicle_routes_vehicle_id (vehicle_id),
	CONSTRAINT fk_vehicle_routes_vehicle
		FOREIGN KEY (vehicle_id) REFERENCES vehicles (id)
		ON UPDATE CASCADE
		ON DELETE CASCADE
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS bookings (
	id INT UNSIGNED NOT NULL AUTO_INCREMENT,
	vehicle_id INT UNSIGNED NOT NULL,
	route_id INT UNSIGNED NULL,
	customer_name VARCHAR(100) NOT NULL,
	phone VARCHAR(20) NOT NULL,
	booking_date DATE NOT NULL,
	status VARCHAR(20) NOT NULL DEFAULT 'Pending',
	PRIMARY KEY (id),
	KEY idx_bookings_vehicle_date_status (vehicle_id, booking_date, status),
	KEY idx_bookings_phone (phone),
	CONSTRAINT fk_bookings_vehicle
		FOREIGN KEY (vehicle_id) REFERENCES vehicles (id)
		ON UPDATE CASCADE
		ON DELETE RESTRICT,
	CONSTRAINT fk_bookings_route
		FOREIGN KEY (route_id) REFERENCES vehicle_routes (id)
		ON UPDATE CASCADE
		ON DELETE SET NULL
) ENGINE = InnoDB;



CREATE TABLE IF NOT EXISTS payments (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    booking_id INT UNSIGNED NOT NULL,
    user_id INT UNSIGNED NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'INR',
    payment_method VARCHAR(50) NOT NULL,
    transaction_reference VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Paid',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMP NULL DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_payments_booking (booking_id),
    UNIQUE KEY uq_payments_transaction_reference (transaction_reference),
    KEY idx_payments_user_id (user_id)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS ratings (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    booking_id INT UNSIGNED NOT NULL,
    vehicle_id INT UNSIGNED NOT NULL,
    customer_phone VARCHAR(20) NOT NULL,
    owner_id INT UNSIGNED NOT NULL,
    rating TINYINT UNSIGNED NOT NULL,
    review VARCHAR(500) NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ratings_booking (booking_id),
    KEY idx_ratings_vehicle (vehicle_id),
    KEY idx_ratings_owner (owner_id),
    CONSTRAINT fk_ratings_booking FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
    CONSTRAINT fk_ratings_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
    CONSTRAINT fk_ratings_owner FOREIGN KEY (owner_id) REFERENCES owners(id) ON DELETE CASCADE,
    CONSTRAINT chk_rating_range CHECK (rating BETWEEN 1 AND 5)
) ENGINE=InnoDB;
