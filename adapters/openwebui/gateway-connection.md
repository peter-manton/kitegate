# Open WebUI through the Kitegate gateway

The recommended setup: Open WebUI talks to models only through the gateway, so
children's chats, titles, tags and follow-ups are all screened, and blocks
show as normal replies.

1. Run the gateway with `KITEGATE_PASSTHROUGH=true` and `KITEGATE_ADULT_USERS`
   (adults' models pass through, for those accounts only) and put Open WebUI on the `kitegate` Docker network.
2. Open WebUI environment:

   ```
   ENABLE_FORWARD_USER_INFO_HEADERS=True   # sends X-OpenWebUI-User-Email, which the gateway reads
   ENABLE_OLLAMA_API=False
   OPENAI_API_BASE_URL=http://kitegate:8080/v1
   OPENAI_API_KEY=<KITEGATE_API_KEY>
   ENABLE_DIRECT_CONNECTIONS=False         # users must not add their own connections
   ```

3. Remove every other connection in Admin Panel > Settings > Connections.
   A connection that bypasses the gateway bypasses deny-by-default too.
4. Admin Panel > Settings > Interface: set the task model to a model served
   through the gateway. Titles and follow-ups for a child's chat are then
   generated on the children's model and screened.
5. Keep web search, code execution and tools off for children's models.

6. A system prompt on the children's model (Admin Panel > Models), or a
   user's own under Settings, is folded into the gateway's system message
   after the core and pack framing, ranked below them. Set
   `KITEGATE_CLIENT_SYSTEM=drop` on the gateway to ignore it instead.

The in-process Open WebUI filter from earlier releases has been removed. If
it is still installed under Admin Panel > Functions, delete it: it refuses
children's models while title generation is on, which this setup needs.
