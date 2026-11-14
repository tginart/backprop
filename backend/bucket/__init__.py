"""
Purpose: Store Backprop events and files in a shared GCS or S3 object prefix.
Input: A gs://, s3:// or file:// installation URI.
Output: An SDK store and a loopback dashboard that reads the prefix with DuckDB.
"""
