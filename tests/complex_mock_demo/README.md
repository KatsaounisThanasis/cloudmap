# Complex Mock Demo

Αυτός ο φάκελος περιέχει τα αρχεία από τη δοκιμή που κάναμε για να δούμε αν το CloudMap μπορεί να εξάγει σωστά (και με ασφάλεια) πολύπλοκες αρχιτεκτονικές από άγνωστα περιβάλλοντα.

## Τα αρχεία:

1. **`01_input_complex_random.json`**: 
   Αυτό είναι το αρχικό "ψεύτικο" JSON που έφτιαξα. Προσομοιώνει την απάντηση του Azure Resource Graph. Περιέχει 5 πόρους (2 WebApps, 1 SQL, 1 Redis, 1 KeyVault). 
   *💡 Παρατήρησε*: Στο `app-backend-random` υπάρχουν `DB_CONNECTION` και `REDIS` που περιέχουν **φανερούς κωδικούς** (π.χ. `Password=S3cretPassword!;`). Επίσης υπάρχει το πεδίο `kubernetes_text` στο AKS με base64 credentials.

2. **`04_scrubbed_output.json`**: 
   Το αποτέλεσμα της εντολής `cloudmap scrub` πάνω στο αρχικό αρχείο.
   *💡 Παρατήρησε τα εξής security features*:
   - Στα App Settings, όπου υπήρχε κωδικός έχει αντικατασταθεί με `Password=REDACTED;`.
   - Το επικίνδυνο πεδίο `kubernetes_text` (που είχε κωδικούς AKS) **έχει διαγραφεί εντελώς**.

## Παραγόμενα αρχεία από το "cloudmap trace":

Όλα τα παρακάτω αρχεία δημιουργήθηκαν με μία μόνο εντολή (`cloudmap trace --from 01_input...`), ζητώντας από το εργαλείο να βγάλει **όλα** τα υποστηριζόμενα format ταυτόχρονα, κάνοντας ιχνηλάτηση από το `app-frontend-random`:

3. **`05_trace_output.mmd`**: Το Mermaid γράφημα. Δείχνει το Blast Radius (Frontend -> Backend -> Redis/SQL/KeyVault).
4. **`06_trace_output.json`**: Η εσωτερική αναπαράσταση (γράφημα) που έφτιαξε το CloudMap (κόμβοι και ακμές) σε JSON.
5. **`07_trace_output.html`**: Το διαδραστικό HTML interactive map του blast radius. Μπορείς να το ανοίξεις στον browser.
6. **`08_trace_output.csv`**: Το CSV export (χρήσιμο για Excel) με τις ακμές (edges).
7. **`09_trace_output.drawio`**: Το αρχείο Draw.io για επεξεργασία του διαγράμματος.

---

# Enterprise Microservices Demo

Αυτά τα αρχεία αφορούν ένα **πάρα πολύ πολύπλοκο** αρχείο που περιέχει μια ολόκληρη Microservices αρχιτεκτονική (19 Azure πόροι). 

- **`10_input_enterprise_architecture.json`**: Το αρχικό αρχείο. Περιέχει:
  - 1 SPA Frontend (το οποίο καλεί ->)
  - 1 API Gateway (το οποίο καλεί ->)
  - 3 APIs (Orders, Inventory, Payments).
  - 1 Auth Service που καλεί Entra ID και CosmosDB.
  - Τα APIs συνδέονται σε Azure SQL, PostgreSQL, Service Bus, Azure Storage, και Key Vaults!
  - 1 AKS Cluster και 1 Background Worker (που επίτηδες ΔΕΝ συνδέονται πουθενά).

Τρέξαμε ιχνηλάτηση (`cloudmap trace`) με αφετηρία ΜΟΝΟ το Frontend (`app-spa-frontend`) και με `--max-hops 10` για να βρει όλο το βάθος:
- **`11_enterprise_trace.mmd`**: Δες πώς το CloudMap χαρτογράφησε αυτόματα ΟΛΗ την αρχιτεκτονική βρίσκοντας ακριβώς **15 resources** και αγνοώντας τα ασύνδετα AKS/Worker!
- **`12_enterprise_trace.html`**: Άνοιξέ το στον browser να δεις το διαδραστικό αποτέλεσμα αυτού του χαμού!

Τέλος, το περάσαμε από τον Scrubber (`cloudmap scrub`):
- **`14_enterprise_scrubbed.json`**: Το εργαλείο βρήκε και "έκρυψε" **42 ονόματα πόρων** και **διέγραψε 8 κωδικούς (credentials)** από connection strings, app settings και kubernetes manifests! (πχ `AccountKey=REDACTED==`).

