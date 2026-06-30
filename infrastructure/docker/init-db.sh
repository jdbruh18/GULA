#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE gula_auth;
    CREATE DATABASE gula_patient;
    CREATE DATABASE gula_study;
EOSQL
