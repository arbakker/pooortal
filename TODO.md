# TODO

- admin interface
  - genereren nieuw token
  - tokens zijn 1 week geldig, dus bij aanmaken geldig_tot column toevoegen aan token tabel
  - bij genereren token, label/email toevoegen en opslaan in database
  - overzicht nog geldige tokens - met label/email, datum aangemaakt
  - overzicht gebruikte tokens - met label/email tonen
- tokens opslaan database (postgres)
- specifieke template voor `/<token>` (token is geldig indien bekend in db, niet verlopen, en niet al bestand geupload)
  - 403/401?
    - wanneer token verlopen is
    - waneer token niet bekend is
    - wanneer niet verlopen is maar bestand al geupload
- chunksize default 50-100MB zetten:
  - in client

---

later:

- voor prod setup nog een nginx reverse proxy
  - chunksize config serverside door in nginx `client_max_body_size ` te zetten
- entraid integratie: zie [az docs](https://learn.microsoft.com/en-us/entra/identity-platform/tutorial-web-app-python-flask-sign-in-out?tabs=workforce-tenants)
- post-create hook validatie upload bestand
- opruimen verlopen tokens