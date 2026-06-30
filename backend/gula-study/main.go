package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/mux"
	_ "github.com/lib/pq"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	amqp "github.com/rabbitmq/amqp091-go"
)

// Event Envelope
type EventEnvelope struct {
	EventID   string      `json:"eventId"`
	EventType string      `json:"eventType"`
	Timestamp string      `json:"timestamp"`
	Source    string      `json:"source"`
	Payload   interface{} `json:"payload"`
}

// FHIR ImagingStudy Metadata
type StudyPayload struct {
	ResourceType    string    `json:"resourceType"`
	ID              string    `json:"id"` // StudyInstanceUID
	Status          string    `json:"status"`
	PatientID       string    `json:"patientId"`
	Started         time.Time `json:"started"`
	AccessionNumber string    `json:"accessionNumber"`
	Modality        string    `json:"modality"`
	StoragePath     string    `json:"storagePath,omitempty"`
	FileSize        int64     `json:"fileSize,omitempty"`
	TenantID        string    `json:"tenantId"`
}

var (
	db           *sql.DB
	rabbitConn   *amqp.Connection
	rabbitChan   *amqp.Channel
	minioClient  *minio.Client
	bucketName   = "gula-dicom"
)

func init() {
	rand.Seed(time.Now().UnixNano())
}

// Connect to Postgres with retry
func connectPostgres(connStr string) *sql.DB {
	var database *sql.DB
	var err error
	for i := 1; i <= 10; i++ {
		database, err = sql.Open("postgres", connStr)
		if err == nil {
			err = database.Ping()
			if err == nil {
				log.Println("gula-study: Connected to Postgres successfully.")
				return database
			}
		}
		log.Printf("gula-study: Postgres connection failed (attempt %d/10): %v. Retrying in 3s...", i, err)
		time.Sleep(3 * time.Second)
	}
	log.Fatal("gula-study: Could not connect to Postgres.")
	return nil
}

// Connect to RabbitMQ with retry
func connectRabbitMQ(url string) (*amqp.Connection, *amqp.Channel) {
	var conn *amqp.Connection
	var ch *amqp.Channel
	var err error
	for i := 1; i <= 10; i++ {
		conn, err = amqp.Dial(url)
		if err == nil {
			ch, err = conn.Channel()
			if err == nil {
				err = ch.ExchangeDeclare("gula.events", "topic", true, false, false, false, nil)
				if err == nil {
					log.Println("gula-study: Connected to RabbitMQ successfully.")
					return conn, ch
				}
			}
		}
		log.Printf("gula-study: RabbitMQ connection failed (attempt %d/10): %v. Retrying in 3s...", i, err)
		time.Sleep(3 * time.Second)
	}
	log.Fatal("gula-study: Could not connect to RabbitMQ.")
	return nil, nil
}

// Connect to MinIO with retry
func connectMinIO(endpoint, accessKey, secretKey string) *minio.Client {
	var client *minio.Client
	var err error
	for i := 1; i <= 10; i++ {
		client, err = minio.New(endpoint, &minio.Options{
			Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
			Secure: false,
		})
		if err == nil {
			// Check live connection
			_, err = client.ListBuckets(context.Background())
			if err == nil {
				log.Println("gula-study: Connected to MinIO successfully.")
				return client
			}
		}
		log.Printf("gula-study: MinIO connection failed (attempt %d/10): %v. Retrying in 3s...", i, err)
		time.Sleep(3 * time.Second)
	}
	log.Fatal("gula-study: Could not connect to MinIO.")
	return nil
}

// Setup Database Schema
func setupSchema() {
	query := `
	CREATE TABLE IF NOT EXISTS studies (
		study_instance_uid VARCHAR(255) PRIMARY KEY,
		patient_id VARCHAR(255) NOT NULL,
		accession_number VARCHAR(255) NOT NULL,
		modality VARCHAR(50) NOT NULL,
		started TIMESTAMP NOT NULL,
		storage_path VARCHAR(500),
		file_size BIGINT,
		tenant_id VARCHAR(100) NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);`
	_, err := db.Exec(query)
	if err != nil {
		log.Fatalf("gula-study: Database schema setup failed: %v", err)
	}
	log.Println("gula-study: Database table \"studies\" verified/created successfully.")
}

// Publish Event Utility
func publishEvent(eventType string, payload interface{}) {
	if rabbitChan == nil {
		log.Println("gula-study: RabbitMQ channel not initialized. Event lost:", eventType)
		return
	}

	eventEnvelope := EventEnvelope{
		EventID:   uuid.New().String(),
		EventType: eventType,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
		Source:    "gula-study",
		Payload:   payload,
	}

	body, err := json.Marshal(eventEnvelope)
	if err != nil {
		log.Printf("gula-study: Error marshalling event: %v", err)
		return
	}

	routingKey := fmt.Sprintf("gula.event.%s", eventType)
	err = rabbitChan.Publish(
		"gula.events",
		routingKey,
		true,
		false,
		amqp.Publishing{
			ContentType:  "application/json",
			DeliveryMode: amqp.Persistent,
			Body:         body,
		},
	)
	if err != nil {
		log.Printf("gula-study: Failed to publish event %s: %v", eventType, err)
	} else {
		log.Printf("gula-study: Published event \"%s\" to routing key \"%s\"", eventType, routingKey)
	}
}

// REST Handlers
func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"service": "gula-study", "status": "UP"})
}

// QIDO-RS: Search Studies
func searchStudiesHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	rows, err := db.Query("SELECT study_instance_uid, patient_id, accession_number, modality, started, storage_path, file_size, tenant_id FROM studies")
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	studies := []StudyPayload{}
	for rows.Next() {
		var s StudyPayload
		err := rows.Scan(&s.ID, &s.PatientID, &s.AccessionNumber, &s.Modality, &s.Started, &s.StoragePath, &s.FileSize, &s.TenantID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.ResourceType = "ImagingStudy"
		s.Status = "available"
		studies = append(studies, s)
	}

	json.NewEncoder(w).Encode(studies)
}

// QIDO-RS: Mock Series
func getSeriesHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	vars := mux.Vars(r)
	studyUID := vars["studyUID"]

	// Retrieve study details to make dynamic series data
	var patientID, modality, tenantID string
	err := db.QueryRow("SELECT patient_id, modality, tenant_id FROM studies WHERE study_instance_uid = $1", studyUID).Scan(&patientID, &modality, &tenantID)
	if err == sql.ErrNoRows {
		http.Error(w, "Study not found", http.StatusNotFound)
		return
	} else if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	series := []map[string]interface{}{
		{
			"resourceType": "ImagingStudySeries",
			"uid":          "1.2.826.0.1.3680043.8.498." + uuid.New().String()[0:8],
			"number":       1,
			"modality":     modality,
			"description":  modality + " Scan Details",
			"instances":    1,
		},
	}
	json.NewEncoder(w).Encode(series)
}

// STOW-RS: Store DICOM Instances
func storeDICOMHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	// Parse multi-part form
	err := r.ParseMultipartForm(100 << 20) // Max 100MB
	if err != nil {
		http.Error(w, "Invalid multipart form: "+err.Error(), http.StatusBadRequest)
		return
	}

	file, header, err := r.FormFile("file")
	if err != nil {
		http.Error(w, "File is required under form parameter 'file'", http.StatusBadRequest)
		return
	}
	defer file.Close()

	// Get form attributes or generate defaults
	patientID := r.FormValue("patientId")
	if patientID == "" {
		patientID = "PT-" + fmt.Sprintf("%d", rand.Intn(90000)+10000)
	}
	tenantID := r.FormValue("tenantId")
	if tenantID == "" {
		tenantID = "HOSPITAL-ALPHA"
	}
	modality := r.FormValue("modality")
	if modality == "" {
		modalities := []string{"CT", "MR", "XR", "US"}
		modality = modalities[rand.Intn(len(modalities))]
	}
	accessionNumber := r.FormValue("accessionNumber")
	if accessionNumber == "" {
		accessionNumber = "ACC-" + fmt.Sprintf("%d", rand.Intn(900000)+100000)
	}

	studyUID := "1.2.826.0.1.3680043.8.498." + fmt.Sprintf("%d.%d", rand.Int63n(1000000), rand.Intn(100000))
	started := time.Now().UTC()

	// Stage 1: Publish StudyReceived Event
	publishEvent("StudyReceived", StudyPayload{
		ResourceType:    "ImagingStudy",
		ID:              studyUID,
		Status:          "registered",
		PatientID:       patientID,
		Started:         started,
		AccessionNumber: accessionNumber,
		Modality:        modality,
		TenantID:        tenantID,
	})

	// Stage 2: Store file in MinIO
	storagePath := fmt.Sprintf("%s/%s/%s", tenantID, patientID, studyUID)
	fileBuffer := new(bytes.Buffer)
	fileSize, err := io.Copy(fileBuffer, file)
	if err != nil {
		http.Error(w, "Failed to read file buffer", http.StatusInternalServerError)
		return
	}

	_, err = minioClient.PutObject(
		context.Background(),
		bucketName,
		storagePath,
		bytes.NewReader(fileBuffer.Bytes()),
		fileSize,
		minio.PutObjectOptions{ContentType: header.Header.Get("Content-Type")},
	)
	if err != nil {
		log.Printf("gula-study: MinIO upload failed: %v", err)
		http.Error(w, "Failed to upload file to Object Storage: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Stage 3: Write metadata to Postgres
	_, err = db.Exec(
		"INSERT INTO studies (study_instance_uid, patient_id, accession_number, modality, started, storage_path, file_size, tenant_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
		studyUID, patientID, accessionNumber, modality, started, storagePath, fileSize, tenantID,
	)
	if err != nil {
		log.Printf("gula-study: Postgres save failed: %v", err)
		http.Error(w, "Failed to save study metadata: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Stage 4: Publish StudyStored Event
	storedPayload := StudyPayload{
		ResourceType:    "ImagingStudy",
		ID:              studyUID,
		Status:          "available",
		PatientID:       patientID,
		Started:         started,
		AccessionNumber: accessionNumber,
		Modality:        modality,
		StoragePath:     storagePath,
		FileSize:        fileSize,
		TenantID:        tenantID,
	}
	publishEvent("StudyStored", storedPayload)

	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"message":          "DICOM study processed successfully",
		"studyInstanceUid": studyUID,
		"patientId":        patientID,
		"accessionNumber":  accessionNumber,
		"storagePath":      storagePath,
	})
}

// WADO-RS: Retrieve instance frame (returns raw stored file)
func getFrameHandler(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	studyUID := vars["studyUID"]

	var storagePath string
	err := db.QueryRow("SELECT storage_path FROM studies WHERE study_instance_uid = $1", studyUID).Scan(&storagePath)
	if err == sql.ErrNoRows {
		http.Error(w, "Study not found", http.StatusNotFound)
		return
	} else if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	object, err := minioClient.GetObject(context.Background(), bucketName, storagePath, minio.GetObjectOptions{})
	if err != nil {
		http.Error(w, "Storage retrieval failed: "+err.Error(), http.StatusInternalServerError)
		return
	}
	defer object.Close()

	w.Header().Set("Content-Type", "application/octet-stream")
	io.Copy(w, object)
}

func main() {
	log.Println("gula-study: Starting microservice...")

	dbUrl := os.Getenv("DATABASE_URL")
	rabbitUrl := os.Getenv("RABBITMQ_URL")
	minioUrl := os.Getenv("MINIO_ENDPOINT")
	minioKey := os.Getenv("MINIO_ACCESS_KEY")
	minioSecret := os.Getenv("MINIO_SECRET_KEY")

	// Fallback to local configs
	if dbUrl == "" { dbUrl = "postgresql://postgres:postgrespassword@localhost:5432/gula_study?sslmode=disable" }
	if rabbitUrl == "" { rabbitUrl = "amqp://guest:guest@localhost:5672/" }
	if minioUrl == "" { minioUrl = "localhost:9000" }
	if minioKey == "" { minioKey = "minioadmin" }
	if minioSecret == "" { minioSecret = "minioadminpassword" }

	// Connect dependencies
	db = connectPostgres(dbUrl)
	defer db.Close()
	setupSchema()

	rabbitConn, rabbitChan = connectRabbitMQ(rabbitUrl)
	defer rabbitConn.Close()
	defer rabbitChan.Close()

	minioClient = connectMinIO(minioUrl, minioKey, minioSecret)

	// Ensure MinIO bucket exists
	ctx := context.Background()
	exists, err := minioClient.BucketExists(ctx, bucketName)
	if err == nil && !exists {
		err = minioClient.MakeBucket(ctx, bucketName, minio.MakeBucketOptions{})
		if err != nil {
			log.Fatalf("gula-study: Could not create bucket: %v", err)
		}
		log.Println("gula-study: MinIO bucket created successfully.")
	} else {
		log.Println("gula-study: MinIO bucket verified.")
	}

	// Router setup
	r := mux.NewRouter()
	r.HandleFunc("/health", healthHandler).Methods("GET")
	
	// DICOMweb routes
	r.HandleFunc("/dicomweb/studies", searchStudiesHandler).Methods("GET")
	r.HandleFunc("/dicomweb/studies", storeDICOMHandler).Methods("POST")
	r.HandleFunc("/dicomweb/studies/{studyUID}/series", getSeriesHandler).Methods("GET")
	r.HandleFunc("/dicomweb/studies/{studyUID}/series/{seriesUID}/instances/{instanceUID}/frames/{frameNumber}", getFrameHandler).Methods("GET")

	port := os.Getenv("PORT")
	if port == "" { port = "3002" }
	log.Printf("gula-study: Server running on port %s", port)
	log.Fatal(http.ListenAndServe(":"+port, r))
}
