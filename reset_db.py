"""
Script to reset PostgreSQL database for AI service.
"""
import os
import psycopg2
from psycopg2 import sql

# Database connection params
DB_HOST = 'localhost'
DB_PORT = 5432
DB_USER = 'postgres'
DB_PASSWORD = 'postgres'
DB_NAME = 'video_production_ai'

def reset_database():
    """Drop and recreate database."""
    # Connect to postgres database to drop/create our database
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database='postgres'
    )
    conn.autocommit = True
    cursor = conn.cursor()
    
    try:
        # Terminate existing connections
        cursor.execute(f"""
            SELECT pg_terminate_backend(pg_stat_activity.pid)
            FROM pg_stat_activity
            WHERE pg_stat_activity.datname = '{DB_NAME}'
              AND pid <> pg_backend_pid();
        """)
        print(f"Terminated existing connections to {DB_NAME}")
        
        # Drop database
        cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(
            sql.Identifier(DB_NAME)
        ))
        print(f"Dropped database: {DB_NAME}")
        
        # Create database
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(
            sql.Identifier(DB_NAME)
        ))
        print(f"Created database: {DB_NAME}")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    reset_database()
    print("\nDatabase reset complete! Now run: python manage.py migrate")
