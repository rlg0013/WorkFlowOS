package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"workflowos/internal/config"
	"workflowos/internal/db"
	"workflowos/internal/handler"
	"workflowos/internal/llm"
	"workflowos/internal/service"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	cfg, err := config.Load("config.yaml")
	if err != nil {
		logger.Error("configuration load failed", "error", err)
		return
	}

	database, err := db.Init(&cfg.Database)
	if err != nil {
		logger.Error("database initialization failed", "error", err)
		return
	}
	defer database.Close()

	eventService := service.NewRepetitionDetector(database)
	workflowService := service.NewWorkflowService(database)
	crmService := service.NewCRMService(database)
	llmClient := llm.NewClient(&cfg.LLM)

	pythonExecutorURL := getEnv("PYTHON_EXECUTOR_URL", "http://localhost:8082")

	h := handler.NewHandler(eventService, workflowService, crmService, llmClient, pythonExecutorURL, cfg.Artifacts.Dir)

	gin.SetMode(getEnv("GIN_MODE", gin.ReleaseMode))
	r := gin.New()
	r.Use(gin.Recovery(), structuredLogger(logger), requestSizeLimit(1<<20), rateLimit(90, time.Minute), corsMiddleware())
	r.GET("/healthz", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"status": "ok", "service": "workflowos"}) })
	r.GET("/readyz", func(c *gin.Context) {
		if err := database.Ping(); err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "not_ready"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "ready"})
	})

	api := r.Group("/api/v1")
	{
		api.POST("/events/ingest", h.IngestEvent)
		api.POST("/demo/observe", h.SeedDemoObservation)
		api.GET("/sequences", h.ListSequences)
		api.POST("/workflows/generate", h.GenerateWorkflow)
		api.GET("/workflows", h.ListWorkflows)
		api.GET("/workflows/:id", h.GetWorkflow)
		api.POST("/workflows/approve", h.ApproveWorkflow)
		api.POST("/workflows/execute", h.ExecuteWorkflow)
		api.GET("/workflows/:id/steps", h.GetWorkflowSteps)
		api.GET("/workflows/:id/proofs", h.GetWorkflowProofs)
		api.GET("/artifacts/*filepath", h.ServeArtifact)

		api.POST("/crm/customers", h.CRMCreateCustomer)
		api.GET("/crm/customers/:email", h.CRMGetCustomer)
		api.PUT("/crm/customers", h.CRMUpsertCustomer)
		api.POST("/crm/customers/:email/notes", h.CRMAddNote)
	}

	// Serve static files from web directory (relative to project root)
	webDir := getEnv("WEB_DIR", "../web")
	r.StaticFile("/", webDir+"/index.html")
	r.Static("/web", webDir)
	r.StaticFile("/openapi.yaml", "../openapi.yaml")

	port := getEnv("PORT", "8080")
	server := &http.Server{Addr: ":" + port, Handler: r, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second}
	go func() {
		logger.Info("server started", "port", port)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("server failed", "error", err)
		}
	}()
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	logger.Info("graceful shutdown started")
	if err := server.Shutdown(ctx); err != nil {
		logger.Error("graceful shutdown failed", "error", err)
	}
}

func structuredLogger(logger *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		started := time.Now()
		c.Next()
		logger.Info("request", "method", c.Request.Method, "path", c.Request.URL.Path, "status", c.Writer.Status(), "latency_ms", time.Since(started).Milliseconds())
	}
}

func requestSizeLimit(maxBytes int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxBytes)
		c.Next()
	}
}

func rateLimit(limit int, window time.Duration) gin.HandlerFunc {
	type visitor struct {
		mu    sync.Mutex
		count int
		reset time.Time
	}
	var visitors sync.Map
	return func(c *gin.Context) {
		key := c.ClientIP()
		now := time.Now()
		current, _ := visitors.LoadOrStore(key, &visitor{reset: now.Add(window)})
		entry := current.(*visitor)
		entry.mu.Lock()
		if now.After(entry.reset) {
			entry.count = 0
			entry.reset = now.Add(window)
		}
		entry.count++
		exceeded := entry.count > limit
		entry.mu.Unlock()
		if exceeded {
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{"error": "rate limit exceeded; retry shortly"})
			return
		}
		c.Next()
	}
}

func corsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Writer.Header().Set("Access-Control-Allow-Origin", "*")
		c.Writer.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		c.Writer.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(204)
			return
		}
		c.Next()
	}
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}
