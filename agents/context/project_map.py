"""Python port of ``agents/src/context/project-map.ts``."""

PROJECT_CONVENTIONS = """
# Joyalty Project Conventions

## Overview
Joyalty is a coffee shop loyalty app. Customers earn stamps per purchase and redeem rewards after 10 purchases.

## Frontend (Expo / React Native)
- **Framework**: Expo SDK 54, React 19, React Native 0.81, TypeScript
- **Routing**: expo-router (file-based). Route files live in `app/`.
  - `app/_layout.tsx` — root layout
  - `app/(tabs)/_layout.tsx` — tab navigator
  - `app/(tabs)/index.tsx` — home/dashboard tab
  - `app/(tabs)/profile.tsx` — profile tab
  - `app/auth/index.tsx` — login screen
  - `app/index.tsx` — entry redirect
- **API layer**: `services/api.ts` — all backend calls go through here
- **State**: React Context in `context/`; hooks in `hooks/`
- **Auth**: Firebase Auth (phone number), token stored in AsyncStorage
- **UI**: Lucide icons, expo-linear-gradient, react-native-reanimated
- **Validation commands**: `npm run typecheck`, `npm run lint`

## Backend (Spring Boot)
- **Framework**: Spring Boot 3.2, Java 17, Maven
- **Source root**: `server/src/main/java/com/joyalty/server/`
- **Package structure**:
  - `controller/` — REST controllers (AuthController, CustomerController, PurchaseController, RewardController)
  - `service/` — business logic (JwtService, CustomerService, FirebaseAuthService)
  - `repository/` — Spring Data JPA repositories
  - `entity/` — JPA entities mapped to PostgreSQL tables
  - `dto/` — request/response DTOs
  - `config/` — SecurityConfig, LoggingConfig
  - `security/` — JwtAuthenticationFilter
- **Config**: `server/src/main/resources/application.properties`
- **Build**: `mvn compile` (or `mvnw compile` on systems with wrapper)
- **Dockerfile**: `server/Dockerfile`

## Database (PostgreSQL)
- **Schema scripts**: `db/` directory
  - `00_reset_database.sql` — drop/recreate
  - `01_create_database.sql` — create DB
  - `02_create_tables.sql` — table definitions
  - `03_insert_sample_data.sql` — seed data
- **Tables**: customers, purchases, rewards
- **Docker**: `docker-compose.yml` runs postgres + spring-boot

## API Endpoints
- POST `/api/auth/login` — phone number login, returns JWT + customerId
- GET  `/api/customer/{customerId}` — get customer data
- POST `/api/purchase/add` — record a purchase
- GET  `/api/customer/qr/mint-reward` — customer mints a single-use reward QR token
- POST `/api/reward/redeem` — tenant-only; redeem a customer's reward QR (requires 10 purchases)

## Patterns to Follow
- New screens: create a file in `app/(tabs)/` or `app/` following expo-router conventions
- New API endpoints: controller -> service -> repository -> entity + DTO
- New DB tables: add migration SQL to `db/` with next sequence number
- All API calls from frontend go through `services/api.ts`
- Auth tokens are passed via Authorization: Bearer header
"""
