# Lavalink — installation et exploitation (dev + Raspberry Pi)

Le bot ne lit jamais l'audio lui-même : il pilote un serveur **Lavalink v4** via
le client **Wavelink**. Lavalink est un processus séparé (Java) qui doit tourner
en parallèle du bot.

## 1. Prérequis

- **Java 17+** (Lavalink v4 l'exige).
  - Debian/Raspberry Pi OS : `sudo apt install openjdk-17-jre-headless`
  - Windows (dev) : Temurin/Adoptium JRE 17+
- Le fichier [`application.yml`](application.yml) fourni dans ce dossier.

## 2. Récupérer Lavalink

Téléchargez `Lavalink.jar` (v4) depuis les releases officielles et placez-le
dans ce dossier `lavalink/`, à côté de `application.yml` :

- https://github.com/lavalink-devs/Lavalink/releases

> Le `.jar` n'est volontairement pas versionné dans le dépôt (binaire lourd).

## 3. Démarrer

Depuis le dossier `lavalink/` (Lavalink lit le `application.yml` du répertoire courant) :

```bash
java -jar Lavalink.jar
```

Au premier lancement, le plugin `youtube-plugin` est téléchargé automatiquement
(connexion Internet requise). Vérifiez dans les logs : `Lavalink is ready to accept connections`.

## 4. Connecter le bot

Dans `.env` (voir `.env.example`), assurez-vous que les valeurs correspondent à
`application.yml` :

```
LAVALINK_HOST=127.0.0.1
LAVALINK_PORT=2333
LAVALINK_PASSWORD=youshallnotpass
```

Le bot se connecte au noeud au chargement du cog `radio`.

## 5. YouTube et `poToken` (si lectures bloquées)

Depuis 2024, YouTube renforce ses protections. Si certaines pistes refusent de
charger (`This video requires login` / âge / bot detection), il faut fournir un
`poToken` + `visitorData` au plugin :

1. Générer le couple avec l'outil officiel : https://github.com/iv-org/youtube-trusted-session-generator
2. Ajouter dans `application.yml` sous `plugins.youtube` :

```yaml
plugins:
  youtube:
    pot:
      token: "VOTRE_PO_TOKEN"
      visitorData: "VOTRE_VISITOR_DATA"
```

Ces valeurs expirent : à renouveler périodiquement.

---

## Raspberry Pi — mesures à prendre

Lavalink (la JVM) est de loin le composant le plus gourmand. Sur un Pi :

1. **Limiter la heap JVM.** Lancez avec un plafond mémoire adapté au modèle de Pi :

   ```bash
   java -Xms64m -Xmx200m -jar Lavalink.jar
   ```

   (Pi 3 : `-Xmx150m`, Pi 4/5 : `-Xmx256m` confortable.)

2. **Audio plus léger** : `resamplingQuality: LOW` est déjà réglé dans
   `application.yml`. Évitez les filtres audio inutiles.

3. **Processus séparés via systemd.** Faites tourner Lavalink et le bot comme
   deux services indépendants pour qu'un redémarrage de l'un n'affecte pas l'autre.

   `/etc/systemd/system/lavalink.service` :

   ```ini
   [Unit]
   Description=Lavalink
   After=network.target

   [Service]
   WorkingDirectory=/home/pi/Hz/lavalink
   ExecStart=/usr/bin/java -Xmx200m -jar Lavalink.jar
   Restart=on-failure
   User=pi

   [Install]
   WantedBy=multi-user.target
   ```

   `/etc/systemd/system/hz-bot.service` :

   ```ini
   [Unit]
   Description=Hz Discord Bot
   After=network.target lavalink.service
   Requires=lavalink.service

   [Service]
   WorkingDirectory=/home/pi/Hz
   ExecStart=/home/pi/Hz/.venv/bin/python bot.py
   Restart=on-failure
   User=pi

   [Install]
   WantedBy=multi-user.target
   ```

   Puis : `sudo systemctl enable --now lavalink hz-bot`.

4. **SQLite + WAL** : déjà activé dans `db.py`. Stockage idéalement sur SSD/USB
   plutôt que sur la carte SD pour limiter l'usure (la base reste petite : ~100
   morceaux actifs).

5. **Cache Wavelink** modéré (`cache_capacity=100`, déjà réglé) pour ne pas
   gonfler la RAM côté bot.

6. **Logs en rotation** : déjà en place côté bot (`logs/bot.log`) et côté Lavalink
   (`application.yml`). Surveillez l'espace disque.

7. **Carte son / Opus** : `opus` est géré par Lavalink, pas besoin de matériel
   audio sur le Pi (tout est réseau vers Discord).
