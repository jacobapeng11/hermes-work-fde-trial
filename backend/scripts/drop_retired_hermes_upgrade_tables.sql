-- Destructive, one-time cleanup for the retired Hermes upgrade control plane.
--
-- This file is intentionally not sourced by init-db/current_schema or any
-- Runtime image reload script. Before running it, freeze Runtime maintenance,
-- take and verify a database backup, then explicitly set the confirmation
-- token in the same mysql session:
--
--   SET @CONFIRM_DROP_RETIRED_HERMES_UPGRADE_TABLES =
--       'DROP_RETIRED_HERMES_UPGRADE_TABLES';
--   SOURCE backend/scripts/drop_retired_hermes_upgrade_tables.sql;

DROP PROCEDURE IF EXISTS drop_retired_hermes_upgrade_tables;

DELIMITER $$

CREATE PROCEDURE drop_retired_hermes_upgrade_tables()
BEGIN
    DECLARE external_reference_count INT DEFAULT 0;
    DECLARE maintenance_frozen_count INT DEFAULT 0;

    IF COALESCE(@CONFIRM_DROP_RETIRED_HERMES_UPGRADE_TABLES, '')
        <> 'DROP_RETIRED_HERMES_UPGRADE_TABLES' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT =
                'explicit retired Hermes upgrade table drop confirmation is required';
    END IF;

    SELECT COUNT(*) INTO maintenance_frozen_count
      FROM runtime_maintenance_control
     WHERE id = 1
       AND maintenance_state = 'frozen'
       AND grant_issuance_enabled = FALSE;
    IF maintenance_frozen_count <> 1 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT =
                'global Runtime maintenance must be frozen before retired table cleanup';
    END IF;

    SELECT COUNT(*) INTO external_reference_count
      FROM information_schema.KEY_COLUMN_USAGE
     WHERE TABLE_SCHEMA = DATABASE()
       AND REFERENCED_TABLE_NAME IN (
            'hermes_runtime_release',
            'hermes_runtime_release_control',
            'hermes_upgrade_job',
            'hermes_upgrade_target',
            'hermes_upgrade_event',
            'hermes_runtime_schema_intent',
            'hermes_runtime_schema_source_facts',
            'hermes_runtime_incomplete_intent_supersession'
       )
       AND TABLE_NAME NOT IN (
            'hermes_runtime_release',
            'hermes_runtime_release_control',
            'hermes_upgrade_job',
            'hermes_upgrade_target',
            'hermes_upgrade_event',
            'hermes_runtime_schema_intent',
            'hermes_runtime_schema_source_facts',
            'hermes_runtime_incomplete_intent_supersession'
       );
    IF external_reference_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT =
                'non-retired tables still reference retired Hermes upgrade tables';
    END IF;

    DROP TABLE IF EXISTS hermes_upgrade_event;
    DROP TABLE IF EXISTS hermes_upgrade_target;
    DROP TABLE IF EXISTS hermes_runtime_incomplete_intent_supersession;
    DROP TABLE IF EXISTS hermes_runtime_schema_source_facts;
    DROP TABLE IF EXISTS hermes_runtime_schema_intent;
    DROP TABLE IF EXISTS hermes_upgrade_job;
    DROP TABLE IF EXISTS hermes_runtime_release_control;
    DROP TABLE IF EXISTS hermes_runtime_release;
END$$

DELIMITER ;

CALL drop_retired_hermes_upgrade_tables();
DROP PROCEDURE IF EXISTS drop_retired_hermes_upgrade_tables;
SET @CONFIRM_DROP_RETIRED_HERMES_UPGRADE_TABLES = NULL;
