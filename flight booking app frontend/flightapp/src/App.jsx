import { useEffect, useMemo, useState } from 'react'
import { SERVER_URL } from './constants'
import './App.css'

const initialBookingForm = {
  travel_class: 'economy',
  seats: 1,
  idempotency_key: '',
}

const NGROK_SKIP_BROWSER_WARNING_HEADER = {
  'ngrok-skip-browser-warning': 'true',
}

const AIRPORT_LOOKUP_RADIUS_METERS = 250000
const AIRPORT_LOOKUP_LIMIT = 6

function App() {
  const [authToken, setAuthToken] = useState(() => localStorage.getItem('access_token') || '')
  const [currentUser, setCurrentUser] = useState(() => localStorage.getItem('current_user') || '')
  const [authMode, setAuthMode] = useState('login')
  const [authForm, setAuthForm] = useState({ username: '', email: '', password: '' })
  const [authStatus, setAuthStatus] = useState({ type: '', message: '' })
  const [destinationSearch, setDestinationSearch] = useState({
    from: '',
    fromAirport: null,
    to: '',
    toAirport: null,
  })
  const [tripResults, setTripResults] = useState(null)
  const [bookingForms, setBookingForms] = useState({})
  const [bookingStatus, setBookingStatus] = useState({})
  const [seatSnapshots, setSeatSnapshots] = useState({})
  const [activePage, setActivePage] = useState('search')
  const [tickets, setTickets] = useState([])
  const [ticketsStatus, setTicketsStatus] = useState('idle')
  const [ticketsError, setTicketsError] = useState('')
  const [refundForms, setRefundForms] = useState({})
  const [refundStatus, setRefundStatus] = useState({})
  const [itineraryStatus, setItineraryStatus] = useState('idle')
  const [itineraryResult, setItineraryResult] = useState(null)
  const [errorMessage, setErrorMessage] = useState('')
  const [isSearching, setIsSearching] = useState(false)

  const isLoggedIn = Boolean(authToken)
  const forecast = tripResults?.weather_forecast || tripResults?.forecast || []
  const placesUnavailable = tripResults?.places?.status === 'unavailable'

  const authHeaders = useMemo(
    () => ({
      Authorization: `Bearer ${authToken}`,
      'Content-Type': 'application/json',
    }),
    [authToken],
  )

  async function request(path, options = {}) {
    const response = await apiFetch(path, options)
    const text = await response.text()
    const data = parseJsonResponse(response, text)

    if (!response.ok) {
      const detail = data?.detail || data?.message || data?.error
      throw new Error(detail || friendlyHttpError(response.status))
    }

    return data
  }

  useEffect(() => {
    if (!authToken || !tripResults?.flights?.length) return undefined

    const controllers = tripResults.flights
      .filter((flight) => flight.flight_instance_id)
      .map((flight) => {
        const controller = new AbortController()
        listenForSeatUpdates(flight.flight_instance_id, authHeaders, controller.signal, (data) => {
          const seatAvailability = getDocumentedSeatAvailability(data)

          if (seatAvailability) {
            setSeatSnapshots((snapshots) => ({
              ...snapshots,
              [flight.flight_instance_id]: seatAvailability,
            }))
          }
        })
        return controller
      })

    return () => {
      controllers.forEach((controller) => controller.abort())
    }
  }, [authHeaders, authToken, tripResults])

  function updateAuthForm(event) {
    setAuthForm((form) => ({ ...form, [event.target.name]: event.target.value }))
  }

  function updateDestinationSearch(event) {
    const field = event.target.name

    setDestinationSearch((search) => ({
      ...search,
      [field]: event.target.value,
      [`${field}Airport`]: null,
    }))
  }

  function selectDestinationAirport(field, airport) {
    setDestinationSearch((search) => ({
      ...search,
      [field]: getAirportSearchValue(airport),
      [`${field}Airport`]: airport,
    }))
  }

async function handleAuth(event) {
    event.preventDefault()
    setAuthStatus({ type: '', message: '' })

    const validationError = validateAuthForm(authForm, authMode)
    if (validationError) {
      setAuthStatus({ type: 'error', message: validationError })
      return
    }

    try {
      if (authMode === 'signup') {
        await request('/signup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: authForm.username.trim(),
            email: authForm.email.trim(),
            password: authForm.password,
          }),
        })
        setAuthStatus({ type: 'success', message: 'Account created. You can log in now.' })
        setAuthMode('login')
        return
      }

      const data = await request('/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: authForm.username,
          password: authForm.password,
        }),
      })

      setAuthToken(data.access_token)
      setCurrentUser(authForm.username)
      localStorage.setItem('access_token', data.access_token)
      localStorage.setItem('current_user', authForm.username)
      setAuthStatus({ type: 'success', message: 'Logged in successfully.' })
    } catch (error) {
      setAuthStatus({ type: 'error', message: error.message || 'Authentication failed.' })
    }
  }

  function logout() {
    setAuthToken('')
    setCurrentUser('')
    setTripResults(null)
    setItineraryResult(null)
    setBookingForms({})
    setBookingStatus({})
    setSeatSnapshots({})
    setActivePage('search')
    setTickets([])
    setTicketsStatus('idle')
    setTicketsError('')
    setRefundForms({})
    setRefundStatus({})
    setErrorMessage('')
    localStorage.removeItem('access_token')
    localStorage.removeItem('current_user')
  }

  async function loadTickets() {
    setTicketsStatus('loading')
    setTicketsError('')

    try {
      const data = await request('/my-tickets', { headers: authHeaders })
      setTickets(Array.isArray(data) ? data : data?.tickets || data?.items || [])
      setTicketsStatus('success')
    } catch (error) {
      setTicketsError(error.message)
      setTicketsStatus('error')
    }
  }

  function openTicketsPage() {
    setActivePage('tickets')
    loadTickets()
  }

  function updateRefundReason(bookingId, reason) {
    setRefundForms((forms) => ({
      ...forms,
      [bookingId]: reason,
    }))
  }

  async function requestRefund(bookingId) {
    const reason = refundForms[bookingId]?.trim()

    if (!reason) {
      setRefundStatus((statuses) => ({
        ...statuses,
        [bookingId]: { type: 'error', message: 'Add a refund reason before submitting.' },
      }))
      return
    }

    setRefundStatus((statuses) => ({
      ...statuses,
      [bookingId]: { type: 'loading', message: 'Submitting refund request...' },
    }))

    try {
      const data = await request(`/my-tickets/${encodeURIComponent(bookingId)}/refund`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ reason }),
      })

      setRefundStatus((statuses) => ({
        ...statuses,
        [bookingId]: { type: 'success', data, message: 'Refund request submitted for review.' },
      }))
      await loadTickets()
    } catch (error) {
      setRefundStatus((statuses) => ({
        ...statuses,
        [bookingId]: { type: 'error', message: error.message },
      }))
    }
  }

  async function searchDestination(event) {
    event.preventDefault()
    if (!isLoggedIn) {
      setErrorMessage('Log in before searching destinations.')
      return
    }

    setIsSearching(true)
    setErrorMessage('')
    setTripResults(null)
    setItineraryResult(null)

    try {
      const params = new URLSearchParams({
        from: getAirportSearchValue(destinationSearch.fromAirport) || destinationSearch.from.trim(),
        to: getAirportSearchValue(destinationSearch.toAirport) || destinationSearch.to.trim(),
      })
      const data = await request(`/flights?${params.toString()}`, { headers: authHeaders })
      setTripResults(data)
      setBookingForms({})
      setBookingStatus({})
      setSeatSnapshots({})
    } catch (error) {
      setErrorMessage(error.message)
    } finally {
      setIsSearching(false)
    }
  }

  function updateBookingForm(flightKey, field, value) {
    setBookingForms((forms) => {
      const existing = forms[flightKey] || initialBookingForm
      const nextKey =
        field === 'travel_class' || field === 'seats' ? '' : existing.idempotency_key
      return {
        ...forms,
        [flightKey]: {
          ...existing,
          [field]: value,
          idempotency_key: nextKey,
        },
      }
    })
  }

  async function bookTicket(flight) {
    const flightKey = getFlightKey(flight)
    const form = bookingForms[flightKey] || initialBookingForm
    const idempotencyKey = form.idempotency_key || createIdempotencyKey()

    setBookingForms((forms) => ({
      ...forms,
      [flightKey]: {
        ...form,
        idempotency_key: idempotencyKey,
      },
    }))

    setBookingStatus((status) => ({
      ...status,
      [flightKey]: {
        type: 'loading',
        message: 'Reserving seats...',
      },
    }))

    try {
      const data = await request('/book-ticket', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          flight_instance_id: flight.flight_instance_id,
          flight_number: flight.flight_number,
          departure_time: flight.departure_time,
          travel_class: form.travel_class,
          seats: Number(form.seats),
          idempotency_key: idempotencyKey,
        }),
      })

      setBookingStatus((status) => ({
        ...status,
        [flightKey]: {
          type: 'pending_payment',
          data,
          idempotency_key: idempotencyKey,
        },
      }))
    } catch (error) {
      setBookingStatus((status) => ({
        ...status,
        [flightKey]: {
          type: 'error',
          message: error.message,
        },
      }))
    }
  }

  async function submitPaymentResult(flight, action) {
    const flightKey = getFlightKey(flight)
    const status = bookingStatus[flightKey]
    const idempotencyKey =
      status?.idempotency_key || bookingForms[flightKey]?.idempotency_key

    if (!idempotencyKey) return

    setBookingStatus((bookingStatuses) => ({
      ...bookingStatuses,
      [flightKey]: {
        ...status,
        type: action === 'complete' ? 'payment_processing' : 'canceling',
      },
    }))

    try {
      const data = await request('/payment-result', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          idempotency_key: idempotencyKey,
          action,
        }),
      })

      setBookingStatus((bookingStatuses) => ({
        ...bookingStatuses,
        [flightKey]: {
          type: action === 'complete' ? 'success' : 'error',
          data: {
            ...status?.data,
            ...data,
            booking: {
              ...status?.data?.booking,
              ...data?.booking,
            },
            payment_session: {
              ...status?.data?.payment_session,
              ...data?.payment_session,
            },
          },
          idempotency_key: idempotencyKey,
          message: action === 'cancel' ? 'Payment cancelled.' : '',
        },
      }))
    } catch (error) {
      setBookingStatus((bookingStatuses) => ({
        ...bookingStatuses,
        [flightKey]: {
          ...status,
          type: 'error',
          message: error.message,
          idempotency_key: idempotencyKey,
        },
      }))
    }
  }

  async function createItinerary() {
    if (!tripResults) return

    setItineraryStatus('loading')
    setItineraryResult(null)

    try {
      const data = await request('/create-itinerary', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          destination: tripResults.destination,
          airport: tripResults.airport,
          weather: tripResults.weather,
          flights: tripResults.flights,
          places: tripResults.places,
          days: 3,
        }),
      })
      setItineraryResult(data)
      setItineraryStatus('success')
    } catch (error) {
      setItineraryResult({ error: error.message })
      setItineraryStatus('error')
    }
  }

  if (!isLoggedIn) {
    return (
      <AuthPage
        authForm={authForm}
        authMode={authMode}
        authStatus={authStatus}
        onAuth={handleAuth}
        onAuthFormChange={updateAuthForm}
        setAuthMode={setAuthMode}
      />
    )
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Flight booking desk</p>
          <h1>Search, book, and build a 3-day trip plan.</h1>
        </div>
        <div className="session">
          <span>Signed in as {currentUser}</span>
          <button className="ghost-button" type="button" onClick={logout}>
            Log out
          </button>
        </div>
      </header>

      <nav className="app-nav" aria-label="App sections">
        <button
          className={activePage === 'search' ? 'active' : ''}
          type="button"
          onClick={() => setActivePage('search')}
        >
          Search flights
        </button>
        <button
          className={activePage === 'tickets' ? 'active' : ''}
          type="button"
          onClick={openTicketsPage}
        >
          My tickets
        </button>
      </nav>

      <section className="workspace single-column">
        {activePage === 'search' ? (
          <section className="content">
            <form className="search-bar panel" onSubmit={searchDestination}>
              <AirportSearchField
                airport={destinationSearch.fromAirport}
                label="From location"
                name="from"
                onChange={updateDestinationSearch}
                onSelect={selectDestinationAirport}
                placeholder="District, state, or country"
                value={destinationSearch.from}
              />
              <AirportSearchField
                airport={destinationSearch.toAirport}
                label="To location"
                name="to"
                onChange={updateDestinationSearch}
                onSelect={selectDestinationAirport}
                placeholder="District, state, or country"
                value={destinationSearch.to}
              />
              <button className="primary-button" type="submit" disabled={isSearching}>
                {isSearching ? 'Searching...' : 'Search flights'}
              </button>
            </form>

            {errorMessage && <p className="notice error">{errorMessage}</p>}
            {isSearching && <p className="empty-state">Loading destination details...</p>}
            {tripResults && (
              <TripResults
                forecast={forecast}
                onBook={bookTicket}
                onCreateItinerary={createItinerary}
                bookingForms={bookingForms}
                bookingStatus={bookingStatus}
                itineraryStatus={itineraryStatus}
                itineraryResult={itineraryResult}
                placesUnavailable={placesUnavailable}
                tripResults={tripResults}
                seatSnapshots={seatSnapshots}
                onPaymentResult={submitPaymentResult}
                updateBookingForm={updateBookingForm}
              />
            )}
          </section>
        ) : (
          <TicketsPage
            onRefresh={loadTickets}
            onRefund={requestRefund}
            refundForms={refundForms}
            refundStatus={refundStatus}
            tickets={tickets}
            ticketsError={ticketsError}
            ticketsStatus={ticketsStatus}
            updateRefundReason={updateRefundReason}
          />
        )}
      </section>
    </main>
  )
}

function AuthPage({ authForm, authMode, authStatus, onAuth, onAuthFormChange, setAuthMode }) {
  const [showPassword, setShowPassword] = useState(false)

  useEffect(() => {
    setShowPassword(false)
  }, [authMode])

  return (
    <main className="auth-page">
      <section className="auth-hero">
        <p className="eyebrow">Flight booking desk</p>
        <h1>{authMode === 'login' ? 'Welcome back.' : 'Create your travel account.'}</h1>
        <p>Sign in to search route-based flights, book seats, and generate itineraries.</p>
      </section>

      <section className="panel auth-card">
        <div className="tabs" role="tablist" aria-label="Authentication mode">
          <button
            className={authMode === 'login' ? 'active' : ''}
            type="button"
            onClick={() => setAuthMode('login')}
          >
            Login
          </button>
          <button
            className={authMode === 'signup' ? 'active' : ''}
            type="button"
            onClick={() => setAuthMode('signup')}
          >
            Signup
          </button>
        </div>

        <form className="stack" onSubmit={onAuth}>
          <label>
            Username
            <input
              name="username"
              value={authForm.username}
              onChange={onAuthFormChange}
              required
              autoComplete="username"
            />
          </label>
          {authMode === 'signup' && (
            <label>
              Email
              <input
                name="email"
                type="email"
                value={authForm.email}
                onChange={onAuthFormChange}
                required
                autoComplete="email"
              />
            </label>
          )}
          <label>
            Password
            <div className="password-input-wrapper">
              <input
                name="password"
                type={showPassword ? 'text' : 'password'}
                value={authForm.password}
                onChange={onAuthFormChange}
                required
                autoComplete={authMode === 'login' ? 'current-password' : 'new-password'}
              />
              <button
                type="button"
                className="password-toggle-button"
                onClick={() => setShowPassword(!showPassword)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </label>
          <button className="primary-button" type="submit">
            {authMode === 'login' ? 'Login' : 'Create account'}
          </button>
        </form>

        {authStatus.message && <p className={`notice ${authStatus.type}`}>{authStatus.message}</p>}
      </section>
    </main>
  )
}

function AirportSearchField({ airport, label, name, onChange, onSelect, placeholder, value }) {
  const [airports, setAirports] = useState([])
  const [lookupStatus, setLookupStatus] = useState('idle')
  const [lookupError, setLookupError] = useState('')
  const [isOpen, setIsOpen] = useState(false)

  useEffect(() => {
    const query = value.trim()

    if (query.length < 3 || airport) {
      setAirports([])
      setLookupStatus('idle')
      setLookupError('')
      return undefined
    }

    const controller = new AbortController()
    const timeoutId = window.setTimeout(async () => {
      setLookupStatus('loading')
      setLookupError('')

      try {
        const results = await findNearbyAirports(query, controller.signal)
        setAirports(results)
        setLookupStatus(results.length ? 'success' : 'empty')
        setIsOpen(true)
      } catch (error) {
        if (controller.signal.aborted) return

        setAirports([])
        setLookupStatus('error')
        setLookupError(error.message || 'Airport lookup failed.')
        setIsOpen(true)
      }
    }, 500)

    return () => {
      window.clearTimeout(timeoutId)
      controller.abort()
    }
  }, [airport, value])

  function handleChange(event) {
    setIsOpen(true)
    onChange(event)
  }

  function handleSelect(selectedAirport) {
    onSelect(name, selectedAirport)
    setIsOpen(false)
  }

  const showDropdown = isOpen && value.trim().length >= 3 && !airport

  return (
    <label className="airport-field">
      {label}
      <input
        name={name}
        value={value}
        onBlur={() => window.setTimeout(() => setIsOpen(false), 150)}
        onChange={handleChange}
        onFocus={() => setIsOpen(true)}
        placeholder={placeholder}
        required
      />
      {airport && (
        <span className="selected-airport">
          Selected: {formatAirportOption(airport)}
        </span>
      )}
      {showDropdown && (
        <div className="airport-menu" role="listbox" aria-label={`${label} airports`}>
          {lookupStatus === 'loading' && <p>Finding nearby airports...</p>}
          {lookupStatus === 'empty' && <p>No nearby airports found. Try a broader place name.</p>}
          {lookupStatus === 'error' && <p>{lookupError}</p>}
          {lookupStatus === 'success' &&
            airports.map((nearbyAirport) => (
              <button
                key={nearbyAirport.id}
                type="button"
                role="option"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => handleSelect(nearbyAirport)}
              >
                <strong>{nearbyAirport.name}</strong>
                <span>
                  {formatAirportCodes(nearbyAirport)} {formatAirportDistance(nearbyAirport.distanceKm)}
                </span>
              </button>
            ))}
        </div>
      )}
    </label>
  )
}

function TripResults({
  tripResults,
  forecast,
  placesUnavailable,
  bookingForms,
  bookingStatus,
  itineraryStatus,
  itineraryResult,
  seatSnapshots,
  updateBookingForm,
  onBook,
  onPaymentResult,
  onCreateItinerary,
}) {
  return (
    <div className="results-grid">
      <section className="panel summary-panel">
        <div>
          <p className="eyebrow">Destination</p>
          <h2>{tripResults.destination || 'Destination'}</h2>
          <p>{tripResults.airport || 'Airport details unavailable'}</p>
        </div>
        <Weather weather={tripResults.weather} />
      </section>

      <section className="panel">
        <div className="section-heading">
          <h2>3-day forecast</h2>
        </div>
        {forecast.length ? (
          <div className="forecast-grid">
            {forecast.map((day) => (
              <article className="mini-card" key={day.date}>
                <strong>{day.date}</strong>
                <span>{formatValue(day.temperature_min)} - {formatValue(day.temperature_max)}</span>
                <span>{formatValue(day.precipitation_sum)} precipitation</span>
                <span>{formatValue(day.wind_speed_max)} wind</span>
              </article>
            ))}
          </div>
        ) : (
          <p className="empty-state">Forecast data is unavailable.</p>
        )}
      </section>

      <section className="panel wide">
        <div className="section-heading">
          <h2>Flights</h2>
        </div>
        {tripResults.flights?.length ? (
          <div className="flight-list">
            {tripResults.flights.map((flight) => (
              <FlightCard
                bookingForm={bookingForms[getFlightKey(flight)] || initialBookingForm}
                bookingStatus={bookingStatus[getFlightKey(flight)]}
                flight={flight}
                key={getFlightKey(flight)}
                seatAvailability={seatSnapshots[flight.flight_instance_id]}
                onBook={onBook}
                onPaymentResult={onPaymentResult}
                updateBookingForm={updateBookingForm}
              />
            ))}
          </div>
        ) : (
          <p className="empty-state">No flights were returned for this destination.</p>
        )}
      </section>

      <section className="panel">
        <div className="section-heading">
          <h2>Places</h2>
        </div>
        {placesUnavailable ? (
          <p className="empty-state">Places are currently unavailable, but flight and weather data are ready.</p>
        ) : (
          <Places places={tripResults.places} />
        )}
      </section>

      <section className="panel itinerary-panel">
        <div className="section-heading">
          <h2>Itinerary</h2>
          <button
            className="primary-button"
            type="button"
            onClick={onCreateItinerary}
            disabled={itineraryStatus === 'loading'}
          >
            {itineraryStatus === 'loading' ? 'Creating...' : 'Create itinerary'}
          </button>
        </div>
        {itineraryResult ? (
          <Itinerary result={itineraryResult} status={itineraryStatus} />
        ) : (
          <p className="empty-state">Generate a 3-day plan from the loaded destination details.</p>
        )}
      </section>
    </div>
  )
}

function Weather({ weather }) {
  const items = [
    ['Temperature', weather?.temperature],
    ['Humidity', weather?.humidity],
    ['Wind speed', weather?.wind_speed],
  ]

  return (
    <div className="metrics">
      {items.map(([label, value]) => (
        <div className="metric" key={label}>
          <span>{label}</span>
          <strong>{formatValue(value)}</strong>
        </div>
      ))}
    </div>
  )
}

function FlightCard({
  flight,
  bookingForm,
  bookingStatus,
  seatAvailability,
  updateBookingForm,
  onBook,
  onPaymentResult,
}) {
  const flightKey = getFlightKey(flight)
  const seats = getDocumentedSeatAvailability(seatAvailability) || getDocumentedSeatAvailability(flight)

  return (
    <article className="flight-card">
      <div className="flight-main">
        <div>
          <p className="eyebrow">{flight.airline || 'Airline'}</p>
          <h3>{flight.flight_number}</h3>
          <p>{flight.from} to {flight.to}</p>
        </div>
        <span className="status-pill">{flight.status || 'status unavailable'}</span>
      </div>

      <div className="flight-details">
        <span>{flight.departure_iata} {formatDateTime(flight.departure_time)}</span>
        <span>{flight.arrival_iata} {formatDateTime(flight.arrival_time)}</span>
        <span>{formatValue(flight.distance_km)} km</span>
        <span>Economy {formatMoney(flight.ticket_price?.economy)}</span>
        <span>Business {formatMoney(flight.ticket_price?.business)}</span>
        <span>Extra {formatMoney(flight.price_difference?.business_extra)}</span>
        <span>Economy seats {formatValue(seats.economy)}</span>
        <span>Business seats {formatValue(seats.business)}</span>
        <span>Instance {formatValue(flight.flight_instance_id)}</span>
      </div>

      <div className="booking-row">
        <label>
          Class
          <select
            value={bookingForm.travel_class}
            onChange={(event) =>
              updateBookingForm(flightKey, 'travel_class', event.target.value)
            }
          >
            <option value="economy">Economy</option>
            <option value="business">Business</option>
          </select>
        </label>
        <label>
          Seats
          <input
            min="1"
            type="number"
            value={bookingForm.seats}
            onChange={(event) => updateBookingForm(flightKey, 'seats', event.target.value)}
          />
        </label>
        <button className="primary-button" type="button" onClick={() => onBook(flight)}>
          Buy ticket
        </button>
      </div>

      {bookingStatus?.type === 'loading' && <p className="notice">Reserving seats...</p>}
      {bookingStatus?.type === 'pending_payment' && (
        <PaymentSession
          data={bookingStatus.data}
          flight={flight}
          onPaymentResult={onPaymentResult}
        />
      )}
      {bookingStatus?.type === 'payment_processing' && <p className="notice">Completing payment...</p>}
      {bookingStatus?.type === 'canceling' && <p className="notice">Canceling payment...</p>}
      {bookingStatus?.type === 'error' && <p className="notice error">{bookingStatus.message}</p>}
      {bookingStatus?.type === 'success' && <BookingReceipt data={bookingStatus.data} />}
    </article>
  )
}

function PaymentSession({ data, flight, onPaymentResult }) {
  const session = data.payment_session
  const amount = getBookingAmount(data)
  const bookingId = getBookingId(data)

  return (
    <div className="payment-session">
      <div>
        <strong>Payment pending</strong>
        <span>Booking ID: {bookingId || 'pending'}</span>
        <span>Amount: {formatMoney(amount)}</span>
        {session?.expires_at && <span>Hold expires: {formatDateTime(session.expires_at)}</span>}
      </div>
      <div className="payment-actions">
        <button className="primary-button" type="button" onClick={() => onPaymentResult(flight, 'complete')}>
          Complete payment
        </button>
        <button className="ghost-button" type="button" onClick={() => onPaymentResult(flight, 'cancel')}>
          Cancel
        </button>
      </div>
    </div>
  )
}

function BookingReceipt({ data }) {
  const remainingSeats = normalizeSeatAvailability(getRemainingSeats(data))

  return (
    <div className="receipt">
      <span>Booking ID: {getBookingId(data) || 'created'}</span>
      <span>Amount: {formatMoney(getBookingAmount(data))}</span>
      <span>
        Remaining seats: Economy {formatValue(remainingSeats.economy)}, Business{' '}
        {formatValue(remainingSeats.business)}
      </span>
      {getBookingStatus(data) && <span>Status: {getBookingStatus(data)}</span>}
    </div>
  )
}

function TicketsPage({
  tickets,
  ticketsStatus,
  ticketsError,
  refundForms,
  refundStatus,
  updateRefundReason,
  onRefund,
  onRefresh,
}) {
  return (
    <section className="tickets-page">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Bought tickets</p>
          <h2>Non-expired tickets</h2>
        </div>
        <button className="ghost-button" type="button" onClick={onRefresh}>
          Refresh
        </button>
      </div>

      {ticketsStatus === 'loading' && <p className="empty-state">Loading your tickets...</p>}
      {ticketsError && <p className="notice error">{ticketsError}</p>}
      {ticketsStatus !== 'loading' && !tickets.length && (
        <p className="empty-state">No non-expired tickets were returned.</p>
      )}

      <div className="ticket-list">
        {tickets.map((ticket) => {
          const bookingId = getBookingId(ticket)
          const status = getBookingStatus(ticket)
          const canRefund = String(status).toLowerCase() === 'confirmed'

          return (
            <article className="ticket-card" key={bookingId || JSON.stringify(ticket)}>
              <div className="ticket-card-main">
                <div>
                  <p className="eyebrow">{ticket.airline || ticket.flight?.airline || 'Flight'}</p>
                  <h3>{ticket.flight_number || ticket.flight?.flight_number || 'Ticket'}</h3>
                  <p>
                    {ticket.from || ticket.flight?.from || 'From'} to{' '}
                    {ticket.to || ticket.flight?.to || 'To'}
                  </p>
                </div>
                <span className="status-pill">{status || 'status unavailable'}</span>
              </div>

              <div className="flight-details">
                <span>Booking ID {formatValue(bookingId)}</span>
                <span>Class {formatValue(ticket.travel_class || ticket.booking?.travel_class)}</span>
                <span>Seats {formatValue(ticket.seats || ticket.booking?.seats)}</span>
                <span>Amount {formatMoney(getBookingAmount(ticket))}</span>
                <span>Departure {formatDateTime(ticket.departure_time || ticket.flight?.departure_time)}</span>
                <span>Instance {formatValue(ticket.flight_instance_id || ticket.flight?.flight_instance_id)}</span>
              </div>

              {canRefund ? (
                <div className="refund-box">
                  <label>
                    Refund reason
                    <textarea
                      value={refundForms[bookingId] || ''}
                      onChange={(event) => updateRefundReason(bookingId, event.target.value)}
                      placeholder="Trip cancelled"
                      rows="3"
                    />
                  </label>
                  <button
                    className="primary-button"
                    type="button"
                    onClick={() => onRefund(bookingId)}
                    disabled={refundStatus[bookingId]?.type === 'loading'}
                  >
                    {refundStatus[bookingId]?.type === 'loading' ? 'Submitting...' : 'Request refund'}
                  </button>
                </div>
              ) : (
                <p className="empty-state">Refund requests are available only for confirmed tickets.</p>
              )}

              {refundStatus[bookingId]?.message && (
                <p className={`notice ${refundStatus[bookingId].type}`}>
                  {refundStatus[bookingId].message}
                </p>
              )}
            </article>
          )
        })}
      </div>
    </section>
  )
}

function Places({ places }) {
  const groups = [
    ['Tourist spots', places?.tourist_spots],
    ['Hotels', places?.hotels],
    ['Restaurants', places?.restaurants],
  ]

  return (
    <div className="place-groups">
      {groups.map(([title, items]) => (
        <div key={title}>
          <h3>{title}</h3>
          {items?.length ? (
            <ul>
              {items.map((item) => (
                <li key={typeof item === 'string' ? item : item.name}>
                  {typeof item === 'string' ? item : item.name || JSON.stringify(item)}
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty-state">No entries returned.</p>
          )}
        </div>
      ))}
    </div>
  )
}

function Itinerary({ result, status }) {
  const isFallback = result.provider === 'local_fallback'
  return (
    <div className={`itinerary-result ${status === 'error' ? 'error-box' : ''}`}>
      {result.provider && (
        <p className="provider-line">
          Provider: {result.provider}
          {result.fallback_from ? `, fallback from ${result.fallback_from}` : ''}
        </p>
      )}
      {isFallback && <p className="notice">Generated without LLM provider support.</p>}
      {result.errors && <p className="notice error">{Array.isArray(result.errors) ? result.errors.join(', ') : result.errors}</p>}
      <pre>{result.itinerary || result.text || result.error || 'No itinerary text returned.'}</pre>
    </div>
  )
}

function friendlyHttpError(status) {
  if (status === 401) return 'Your session expired. Please log in again.'
  if (status === 404) return 'That city was not found. Try another destination name.'
  if (status === 503) return 'The service is temporarily unavailable. Please retry.'
  return 'The request could not be completed.'
}

function validateAuthForm(form, authMode) {
  const username = form.username.trim()
  const password = form.password

  if (!username) return 'Enter your username.'
  if (!password) return 'Enter your password.'

  if (authMode === 'signup') {
    const email = form.email.trim()

    if (!isValidEmail(email)) return 'Enter a valid email address.'
    if (password.length < 8) return 'Password must be at least 8 characters long.'
    if (!/[A-Za-z]/.test(password) || !/\d/.test(password)) {
      return 'Password must include at least one letter and one number.'
    }
  }

  return ''
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)
}

function apiFetch(path, options = {}) {
  return fetch(`${SERVER_URL}${path}`, {
    ...options,
    headers: buildRequestHeaders(options.headers),
  })
}

async function findNearbyAirports(query, signal) {
  const place = await geocodePlace(query, signal)
  if (!place) return []

  const overpassQuery = `
    [out:json][timeout:20];
    (
      node["aeroway"="aerodrome"](around:${AIRPORT_LOOKUP_RADIUS_METERS},${place.lat},${place.lon});
      way["aeroway"="aerodrome"](around:${AIRPORT_LOOKUP_RADIUS_METERS},${place.lat},${place.lon});
      relation["aeroway"="aerodrome"](around:${AIRPORT_LOOKUP_RADIUS_METERS},${place.lat},${place.lon});
    );
    out center tags;
  `
  const params = new URLSearchParams({ data: overpassQuery })
  const response = await fetch(`https://overpass-api.de/api/interpreter?${params.toString()}`, {
    signal,
  })

  if (!response.ok) {
    throw new Error('please try again')
  }

  const data = await response.json()
  const airportsById = new Map()

  ;(data.elements || [])
    .map((element) => normalizeAirportElement(element, place))
    .filter(Boolean)
    .forEach((airport) => {
      if (!airportsById.has(airport.id)) airportsById.set(airport.id, airport)
    })

  return Array.from(airportsById.values())
    .sort((first, second) => first.distanceKm - second.distanceKm)
    .slice(0, AIRPORT_LOOKUP_LIMIT)
}

async function geocodePlace(query, signal) {
  const params = new URLSearchParams({
    q: query,
    format: 'jsonv2',
    limit: '1',
  })
  const response = await fetch(`https://nominatim.openstreetmap.org/search?${params.toString()}`, {
    headers: { Accept: 'application/json' },
    signal,
  })

  if (!response.ok) {
    throw new Error('please try again')
  }

  const [place] = await response.json()
  if (!place) return null

  return {
    lat: Number(place.lat),
    lon: Number(place.lon),
  }
}

function normalizeAirportElement(element, place) {
  const tags = element.tags || {}
  const lat = Number(element.lat ?? element.center?.lat)
  const lon = Number(element.lon ?? element.center?.lon)
  const name = tags.name || tags['name:en'] || tags.iata || tags.icao

  if (!name || Number.isNaN(lat) || Number.isNaN(lon)) return null

  return {
    id: `${element.type}-${element.id}`,
    name,
    iata: tags.iata || tags['ref:iata'] || '',
    icao: tags.icao || tags['ref:icao'] || '',
    lat,
    lon,
    distanceKm: getDistanceKm(place.lat, place.lon, lat, lon),
  }
}

function getDistanceKm(fromLat, fromLon, toLat, toLon) {
  const earthRadiusKm = 6371
  const latDelta = toRadians(toLat - fromLat)
  const lonDelta = toRadians(toLon - fromLon)
  const firstLat = toRadians(fromLat)
  const secondLat = toRadians(toLat)
  const haversine =
    Math.sin(latDelta / 2) ** 2 +
    Math.cos(firstLat) * Math.cos(secondLat) * Math.sin(lonDelta / 2) ** 2

  return earthRadiusKm * 2 * Math.atan2(Math.sqrt(haversine), Math.sqrt(1 - haversine))
}

function toRadians(value) {
  return (value * Math.PI) / 180
}

function getAirportSearchValue(airport) {
  if (!airport) return ''

  return airport.name
}

function formatAirportOption(airport) {
  return `${airport.name}${formatAirportCodes(airport) ? ` (${formatAirportCodes(airport)})` : ''}`
}

function formatAirportCodes(airport) {
  return [airport.iata, airport.icao].filter(Boolean).join(' / ')
}

function formatAirportDistance(distanceKm) {
  if (!Number.isFinite(distanceKm)) return ''

  return `${Math.round(distanceKm)} km away`
}

function buildRequestHeaders(headers) {
  return {
    ...NGROK_SKIP_BROWSER_WARNING_HEADER,
    ...normalizeHeaders(headers),
  }
}

function normalizeHeaders(headers) {
  if (!headers) return {}
  if (headers instanceof Headers) return Object.fromEntries(headers.entries())
  if (Array.isArray(headers)) return Object.fromEntries(headers)

  return headers
}

function parseJsonResponse(response, text) {
  if (!text) return null

  const contentType = response.headers.get('content-type') || ''
  const trimmedText = text.trim()
  const looksLikeHtml = /^<!doctype html/i.test(trimmedText) || /^<html[\s>]/i.test(trimmedText)

  if (looksLikeHtml) {
    throw new Error(
      'Received HTML instead of JSON. This usually means ngrok returned its browser warning page.',
    )
  }

  if (!contentType.includes('application/json')) {
    throw new Error(`Expected JSON response but received ${contentType || 'unknown content type'}.`)
  }

  try {
    return JSON.parse(text)
  } catch {
    throw new Error('Received an invalid JSON response from the server.')
  }
}

async function listenForSeatUpdates(flightInstanceId, authHeaders, signal, onSeatUpdate) {
  try {
    const response = await apiFetch(
      `/flight-instances/${encodeURIComponent(flightInstanceId)}/seat-events`,
      { headers: authHeaders, signal },
    )
    if (!response.ok || !response.body) return

    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
    let buffer = ''

    while (!signal.aborted) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value

      const messages = buffer.split('\n\n')
      buffer = messages.pop() || ''

      messages.forEach((message) => {
        const dataLine = message
          .split('\n')
          .find((line) => line.startsWith('data:'))

        if (!dataLine) return

        try {
          onSeatUpdate(JSON.parse(dataLine.replace(/^data:\s*/, '')))
        } catch {
          // Ignore malformed SSE payloads while keeping the stream alive.
        }
      })
    }
  } catch (error) {
    if (!signal.aborted) {
      console.warn('Seat event stream failed', error)
    }
  }
}

function getFlightKey(flight) {
  return flight.flight_instance_id || `${flight.flight_number}-${flight.departure_time}`
}

function formatValue(value) {
  return value === undefined || value === null || value === '' ? 'N/A' : value
}

function formatMoney(value) {
  if (value === undefined || value === null || value === '') return 'N/A'
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  }).format(value)
}

function formatDateTime(value) {
  if (!value) return 'N/A'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function getBookingId(data) {
  return data?.booking_id || data?.id || data?.booking?.booking_id || data?.booking?.id
}

function getBookingAmount(data) {
  return (
    data?.amount ??
    data?.total_amount ??
    data?.total_price ??
    data?.total_cost ??
    data?.price ??
    data?.fare ??
    data?.paid_amount ??
    data?.payment_amount ??
    data?.booking_amount ??
    data?.final_amount ??
    data?.booking?.amount ??
    data?.booking?.total_amount ??
    data?.booking?.total_price ??
    data?.booking?.total_cost ??
    data?.booking?.price ??
    data?.booking?.fare ??
    data?.booking?.paid_amount ??
    data?.booking?.payment_amount ??
    data?.booking?.booking_amount ??
    data?.booking?.final_amount ??
    data?.payment?.amount ??
    data?.payment?.total_amount ??
    data?.payment?.total_price ??
    data?.payment?.paid_amount ??
    data?.payment_session?.amount ??
    data?.payment_session?.total_amount ??
    data?.payment_session?.total_price
  )
}

function getRemainingSeats(data) {
  return (
    getDocumentedSeatAvailability(data?.remaining_seats) ||
    getDocumentedSeatAvailability(data?.seat_availability) ||
    getDocumentedSeatAvailability(data?.booking?.remaining_seats) ||
    getDocumentedSeatAvailability(data?.booking?.seat_availability) ||
    getDocumentedSeatAvailability(data?.flight) ||
    getDocumentedSeatAvailability(data)
  )
}

function normalizeSeatAvailability(seats) {
  return {
    economy: seats?.economy,
    business: seats?.business,
  }
}

function getBookingStatus(data) {
  return (
    data?.status ||
    data?.booking_status ||
    data?.booking?.status ||
    data?.payment_status ||
    data?.refund_status
  )
}

function getDocumentedSeatAvailability(source) {
  if (!source) return null

  if (hasDocumentedSeatFields(source)) {
    return source
  }

  if (hasDocumentedSeatFields(source.seat_availability)) {
    return source.seat_availability
  }

  if (hasDocumentedSeatFields(source.data)) {
    return source.data
  }

  if (hasDocumentedSeatFields(source.data?.seat_availability)) {
    return source.data.seat_availability
  }

  if (hasDocumentedSeatFields(source.payload)) {
    return source.payload
  }

  if (hasDocumentedSeatFields(source.payload?.seat_availability)) {
    return source.payload.seat_availability
  }

  return null
}

function hasDocumentedSeatFields(value) {
  return value && (value.economy !== undefined || value.business !== undefined)
}

function createIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()

  return `booking-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export default App
