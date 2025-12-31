# Quick Reference Card

## Admin Commands

```bash
# Clear cache
/admin cache_clear YOUR_TOKEN

# Clean database
/admin db_cleanup YOUR_TOKEN

# Check queue
/admin queue_status YOUR_TOKEN

# Restart bot
/admin restart YOUR_TOKEN
```

## Health Monitoring

```bash
# Full health check
curl http://localhost:5001/health | jq

# Metrics only
curl http://localhost:5001/metrics | jq

# Check memory
curl http://localhost:5001/health | jq '.details.memory_mb'

# Check queue
curl http://localhost:5001/health | jq '.details.groq_queue_size'
```

## Environment Variables

```env
# Required
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=...
GROQ_API_KEY=...

# At least one news API
NEWSDATA_API_KEY=...
MARKETAUX_API_KEY=...

# New: Admin token
ADMIN_TOKEN=your-secure-token

# New: Webhook (optional)
USE_WEBHOOK=false
WEBHOOK_URL=https://your-domain.com/webhook/telegram
WEBHOOK_PORT=5001
```

## Database Cleanup

**Automatic:** Every 10 news cycles (~5 hours)
- Sent articles: >30 days old
- Processed articles: Keep max 2000
- Feedback: >90 days old

**Manual:**
```
/admin db_cleanup YOUR_TOKEN
```

## Logs

```bash
# View logs
tail -f bot.log

# Search for errors
grep ERROR bot.log

# Search for cleanup
grep "Cleaned up\|Pruned" bot.log

# JSON format (production)
cat bot.log | jq
```

## Troubleshooting

| Issue | Solution |
|---|---|
| High memory | `/admin cache_clear TOKEN` |
| Queue buildup | Check `/admin queue_status` |
| Posts failing | Check logs for rate limits |
| Database growing | Verify cleanup is running |
| Webhook not working | Check logs, fallback to polling |

## Performance Tuning

```env
# High volume
NEWS_CHECK_INTERVAL_SECONDS=900
TWEET_POST_DELAY_SECONDS=60
USE_WEBHOOK=true

# Low resource
NEWS_CHECK_INTERVAL_SECONDS=3600
TWEET_POST_DELAY_SECONDS=300
USE_WEBHOOK=false
```

## Key Files

- `app.py` - Main application
- `requirements.txt` - Dependencies
- `bot.log` - Application logs
- `bot_data.db` - SQLite database
- `.env` - Configuration

## Documentation

- `IMPROVEMENTS.md` - Technical details
- `SETUP_GUIDE.md` - Setup & usage
- `DATABASE_CLEANUP.md` - Database maintenance
- `CHANGES_SUMMARY.md` - What changed

## Deployment

```bash
# Install
pip install -r requirements.txt

# Run
python app.py

# Docker
docker build -t crypto-bot .
docker run -e TELEGRAM_BOT_TOKEN=... crypto-bot
```

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Full health check |
| `/metrics` | GET | Metrics only |
| `/cache/clear` | POST | Clear cache |
| `/webhook/telegram` | POST | Telegram webhook |

## Useful Commands

```bash
# Generate secure token
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Check database size
ls -lh bot_data.db

# Backup database
cp bot_data.db bot_data.db.backup

# View database tables
sqlite3 bot_data.db ".tables"

# Count articles
sqlite3 bot_data.db "SELECT COUNT(*) FROM processed_articles;"
```

## Rate Limits

- Groq API: 30 calls/minute
- News APIs: 10 calls/minute
- Telegram: Built-in rate limiting
- Twitter: Built-in rate limiting

## Timeouts

- Groq API: 60 seconds
- User questions: 45 seconds
- News APIs: 15 seconds
- Image validation: 7 seconds

## Database Retention

- Sent articles: 30 days
- Processed articles: 2000 max
- Feedback: 90 days
- User stats: Indefinite

## Support

1. Check logs: `tail -f bot.log`
2. Check health: `curl http://localhost:5001/health`
3. Check stats: `/stats` in Telegram
4. Read docs: See SETUP_GUIDE.md
