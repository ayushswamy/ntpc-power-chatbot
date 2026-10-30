# Adding the chat widget to the ASP.NET application

Audience: the developer who maintains the ASP.NET operations-report application.

The chatbot runs as a separate service (the "chatbot server", Python/FastAPI, port 8000).
The ASP.NET application needs **two small changes**:

1. one `<script>` line in the page layout, which shows the chat button, and
2. a small **forwarding handler** at `/chatbot/chat` that passes questions to the chatbot
   server and adds the secret API key **on the server**.

```
Browser ──POST /chatbot/chat──► ASP.NET app (adds X-API-Key) ──► chatbot server :8000/chat
        ◄──── streamed answer (Server-Sent Events) ◄────────────────────────────────
```

The API key must never appear in a page, a script tag or browser code. With the forwarding
handler, only users who can open the ASP.NET application can use the chatbot, and the key
stays in `web.config`.

---

## 1. Add the script tag

In the master page / layout (for example `Site.Master` or `Views/Shared/_Layout.cshtml`), just
before `</body>`:

```html
<script src="http://CHATBOT-SERVER:8000/widget/ntpc-chat.js" data-endpoint="/chatbot" defer></script>
```

| Attribute | Meaning |
|---|---|
| `src` | The widget file, served by the chatbot server (it also loads its chart library from there) |
| `data-endpoint="/chatbot"` | The widget sends questions to `/chatbot/chat` **on the ASP.NET site** (the handler below) |
| `data-title` (optional) | Panel title, default "Power Data Assistant" |
| `data-accent` (optional) | Colour as `#rrggbb`, default `#1d5f7c` |
| `data-position` (optional) | `right` (default) or `left` |

To show the chat only on some pages, add the tag to those pages instead of the layout.

If the site sends a **Content-Security-Policy** header, allow the chatbot server in
`script-src` (for example `script-src 'self' http://CHATBOT-SERVER:8000`).

---

## 2. Add the forwarding handler

### Option A: ASP.NET (.NET Framework, Web Forms or MVC 5)

**`App_Code/ChatbotProxy.cs`** (or any class library in the project):

```csharp
using System.Configuration;
using System.Net;
using System.Web;

/// Forwards POST /chatbot/chat to the chatbot server, adds the API key, and streams the answer back.
public class ChatbotProxy : IHttpHandler
{
    public bool IsReusable { get { return true; } }

    public void ProcessRequest(HttpContext context)
    {
        // Only logged-in users of this application may use the chatbot (remove if the app has no login).
        if (!context.Request.IsAuthenticated) { context.Response.StatusCode = 401; return; }
        if (context.Request.HttpMethod != "POST") { context.Response.StatusCode = 405; return; }

        var target = ConfigurationManager.AppSettings["ChatbotUrl"].TrimEnd('/') + "/chat";
        var request = (HttpWebRequest)WebRequest.Create(target);
        request.Method = "POST";
        request.ContentType = "application/json";
        request.Headers["X-API-Key"] = ConfigurationManager.AppSettings["ChatbotApiKey"];
        request.Headers["X-User"] = context.User.Identity.Name;    // who asked - recorded in the chatbot audit log
        request.Timeout = 600000;            // answers can take a minute or more on a CPU-only server
        request.ReadWriteTimeout = 600000;
        request.AllowReadStreamBuffering = false;
        using (var body = request.GetRequestStream()) { context.Request.InputStream.CopyTo(body); }

        HttpWebResponse response;
        try { response = (HttpWebResponse)request.GetResponse(); }
        catch (WebException ex) when (ex.Response != null) { response = (HttpWebResponse)ex.Response; }

        context.Response.StatusCode = (int)response.StatusCode;
        context.Response.ContentType = response.ContentType;
        context.Response.BufferOutput = false;                       // send each piece as soon as it arrives
        context.Response.Cache.SetCacheability(HttpCacheability.NoCache);
        using (response)
        using (var stream = response.GetResponseStream())
        {
            var buffer = new byte[4096];
            int read;
            while ((read = stream.Read(buffer, 0, buffer.Length)) > 0)
            {
                context.Response.OutputStream.Write(buffer, 0, read);
                context.Response.Flush();
            }
        }
    }
}
```

**`web.config`**:

```xml
<configuration>
  <appSettings>
    <add key="ChatbotUrl" value="http://CHATBOT-SERVER:8000" />
    <!-- the API_KEY value from the chatbot server's .env file; keep web.config out of source control or encrypt this section -->
    <add key="ChatbotApiKey" value="PASTE-THE-API-KEY-HERE" />
  </appSettings>

  <location path="chatbot/chat">
    <system.webServer>
      <!-- compression would hold back the streamed answer until it is complete -->
      <urlCompression doDynamicCompression="false" />
      <handlers>
        <add name="ChatbotProxy" verb="POST" path="chatbot/chat" type="ChatbotProxy" />
      </handlers>
    </system.webServer>
  </location>
</configuration>
```

### Option B: ASP.NET Core

```csharp
// Program.cs
builder.Services.AddHttpClient("chatbot", c => {
    c.BaseAddress = new Uri(builder.Configuration["Chatbot:Url"]!);          // http://CHATBOT-SERVER:8000
    c.Timeout = TimeSpan.FromMinutes(10);
});

app.MapPost("/chatbot/chat", async (HttpContext ctx, IHttpClientFactory factory, IConfiguration config) =>
{
    var client = factory.CreateClient("chatbot");
    using var request = new HttpRequestMessage(HttpMethod.Post, "chat")
    {
        Content = new StreamContent(ctx.Request.Body)
    };
    request.Content.Headers.ContentType = new("application/json");
    request.Headers.Add("X-API-Key", config["Chatbot:ApiKey"]);               // from appsettings / user secrets
    request.Headers.Add("X-User", ctx.User.Identity?.Name ?? "");              // who asked - for the audit log

    using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
    ctx.Response.StatusCode = (int)response.StatusCode;
    ctx.Response.ContentType = response.Content.Headers.ContentType?.ToString();
    ctx.Response.Headers.CacheControl = "no-cache";
    await using var stream = await response.Content.ReadAsStreamAsync();
    var buffer = new byte[4096];
    int read;
    while ((read = await stream.ReadAsync(buffer)) > 0)
    {
        await ctx.Response.Body.WriteAsync(buffer.AsMemory(0, read));
        await ctx.Response.Body.FlushAsync();
    }
}).RequireAuthorization();
```

> These handlers are templates written for this project; they could not be compiled on the
> development PC (no .NET installed). The web team should build and test them once.

---

## 3. Check that it works

1. On the chatbot server, the API is running:
   `http://CHATBOT-SERVER:8000/health` → `{"status": "ok", ...}`
2. Open any page with the script tag; the round chat button appears bottom-right.
3. Ask "Current total generation of NTPC". You should see "Understanding your question…",
   then the answer with a **Live · as of HH:MM** badge.
4. If the answer says *Missing or invalid API key*: check `ChatbotApiKey` in `web.config`
   matches `API_KEY` in the chatbot server's `.env`.
5. If the answer appears only at the very end (no "typing"): dynamic compression or output
   buffering is still on for `/chatbot/chat`.

## What the widget sends and receives

- Request: `POST /chatbot/chat` with JSON `{"message": "...", "session_id": "..." | null}`.
- Response: Server-Sent Events: `status`, `token` (answer text in pieces), `answer` (full answer
  with table / chart / sources / data time), `error`, `done`. See `app/api/main.py`.
- The same endpoint can later serve a mobile app or voice input (speech converted to text first).
