# Optional site password

Put a file called `site.caddy` here to ask every visitor for a username and password before
they reach BookCRM (handy while a partner reviews a demo). Files ending `.caddy` in this folder
are git-ignored.

1. Make a password hash (the password is typed, not saved):

   ```sh
   docker compose -f docker-compose.prod.yml --env-file .env.production run --rm caddy caddy hash-password
   ```

2. Create `deploy/auth/site.caddy`, with the username and the hash it printed:

   ```
   basic_auth {
   	partner $2a$14$...the-hash...
   }
   ```

3. Reload: `docker compose -f docker-compose.prod.yml --env-file .env.production restart caddy`

Delete the file and restart Caddy to remove the password. While it is on, API clients can't
use their own `Authorization` header through the site.
