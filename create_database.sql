-- Create database for Django AI service
CREATE DATABASE video_production_ai
    WITH 
    OWNER = postgres
    ENCODING = 'UTF8'
    LC_COLLATE = 'English_United States.1252'
    LC_CTYPE = 'English_United States.1252'
    TABLESPACE = pg_default
    CONNECTION LIMIT = -1;

COMMENT ON DATABASE video_production_ai
    IS 'Database for AutomationGenVideo AI service (Django)';
