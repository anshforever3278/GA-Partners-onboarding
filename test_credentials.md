# Test Credentials

## Seeded Users
- Username: `admin` / Password: `admin123` (Role: PM Manager)
- Username: `pm.jane` / Password: `pm123` (Role: PM)
- Username: `pa.mike` / Password: `pa123` (Role: PA)

Auth endpoint: POST /api/auth/login
Returns JWT in `token` field; send as `Authorization: Bearer <token>`.
