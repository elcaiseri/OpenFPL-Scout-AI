/**
 * FPL Scout Team Application
 * Main JavaScript file for handling team data display and interactions
 */

// Application Configuration
const CONFIG = {
    dataPath: 'data/internal/scout_team/',
    filePrefix: 'gw_',
    fileExtension: '.json',
    maxGameweeks: 38,
    defaultGameweek: 38
};

// Application State
const appState = {
    currentGameweek: CONFIG.defaultGameweek,
    availableGameweeks: [],
    currentData: null,
    isLoading: false
};

// Utility Functions
const utils = {
    /**
     * Debounce function to limit rapid function calls
     */
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    },

    /**
     * Safely parse JSON with error handling
     */
    async safeJsonParse(response) {
        try {
            return await response.json();
        } catch (error) {
            throw new Error('Invalid JSON response');
        }
    },

    /**
     * Format player data for display
     */
    formatPlayerData(player) {
        return {
            ...player,
            expected_points: parseFloat(player.expected_points) || 0,
            web_name: player.web_name || 'Unknown',
            team_name: player.team_name || 'Unknown',
            opponent_team_name: player.opponent_team_name || 'Unknown',
            element_type: player.element_type || 'Unknown',
            role: player.role || null,
            was_home: Boolean(player.was_home)
        };
    }
};

// DOM Elements Cache
const domElements = {
    get pitch() { return document.getElementById('pitch'); },
    get gameweekInfo() { return document.getElementById('gameweek-info'); },
    get gameweekSelect() { return document.getElementById('gameweek-select'); },
    get totalPoints() { return document.getElementById('total-points'); },
    get playerCount() { return document.getElementById('player-count'); },
    get credits() { return document.getElementById('credits'); },
    get screenshotBtn() { return document.getElementById('screenshot-btn'); },
    get container() { return document.querySelector('.container'); }
};

// Player Card Creation
const playerCardRenderer = {
    /**
     * Create HTML for a player card
     */
    createPlayerCard(player) {
        const formattedPlayer = utils.formatPlayerData(player);
        const roleClass = this.getRoleClass(formattedPlayer.role);
        const roleBadge = this.createRoleBadge(formattedPlayer.role);
        const homeIndicator = formattedPlayer.was_home ? 'home' : 'away';

        return `
            <div class="player-card ${roleClass}" 
                 data-player='${JSON.stringify(formattedPlayer)}'
                 tabindex="0"
                 role="button"
                 aria-label="Player: ${formattedPlayer.web_name}, Expected points: ${formattedPlayer.expected_points.toFixed(2)}">
                ${roleBadge}
                <div class="player-name">${formattedPlayer.web_name}</div>
                <div class="team-name">${formattedPlayer.team_name}</div>
                <div class="fixture">
                    vs ${formattedPlayer.opponent_team_name} 
                    <span class="home-indicator ${homeIndicator}" 
                          aria-label="${formattedPlayer.was_home ? 'Home' : 'Away'} game"></span>
                </div>
                <div class="expected-points">${formattedPlayer.expected_points.toFixed(2)}</div>
            </div>
        `;
    },

    /**
     * Get CSS class for player role
     */
    getRoleClass(role) {
        switch (role) {
            case 'captain': return 'captain';
            case 'vice': return 'vice';
            default: return '';
        }
    },

    /**
     * Create role badge HTML
     */
    createRoleBadge(role) {
        switch (role) {
            case 'captain':
                return '<div class="role-badge captain" aria-label="Captain">C</div>';
            case 'vice':
                return '<div class="role-badge vice" aria-label="Vice Captain">VC</div>';
            default:
                return '';
        }
    }
};

// Team Rendering
const teamRenderer = {
    /**
     * Render the complete team formation
     */
    renderTeam(data) {
        if (!data || !data.scout_team || !Array.isArray(data.scout_team)) {
            throw new Error('Invalid team data structure');
        }

        const positions = this.groupPlayersByPosition(data.scout_team);
        const pitchHTML = this.createFormationHTML(positions);
        
        domElements.pitch.innerHTML = pitchHTML;
        domElements.pitch.classList.add('fade-in');
    },

    /**
     * Group players by their positions
     */
    groupPlayersByPosition(players) {
        const positions = {
            'Goalkeeper': [],
            'Defender': [],
            'Midfielder': [],
            'Forward': []
        };

        players.forEach(player => {
            const formattedPlayer = utils.formatPlayerData(player);
            if (positions[formattedPlayer.element_type]) {
                positions[formattedPlayer.element_type].push(formattedPlayer);
            }
        });

        // Sort each position by expected points (descending)
        Object.keys(positions).forEach(position => {
            positions[position].sort((a, b) => b.expected_points - a.expected_points);
        });

        return positions;
    },

    /**
     * Create HTML for the formation layout
     */
    createFormationHTML(positions) {
        const positionOrder = ['Goalkeeper', 'Defender', 'Midfielder', 'Forward'];
        
        return positionOrder.map(position => {
            const players = positions[position];
            if (!players || players.length === 0) return '';

            const positionClass = position.toLowerCase();
            const positionName = this.getPluralPositionName(position);

            return `
                <div class="position-label ${positionClass}">${positionName}</div>
                <div class="formation-line">
                    ${players.map(player => playerCardRenderer.createPlayerCard(player)).join('')}
                </div>
            `;
        }).join('');
    },

    /**
     * Get plural form of position names
     */
    getPluralPositionName(position) {
        const pluralMap = {
            'Goalkeeper': 'Goalkeepers',
            'Defender': 'Defenders',
            'Midfielder': 'Midfielders',
            'Forward': 'Forwards'
        };
        return pluralMap[position] || position;
    }
};

// Statistics Management
const statisticsManager = {
    /**
     * Update team statistics display
     */
    updateStatistics(data) {
        if (!data || !data.scout_team) return;

        const stats = this.calculateStatistics(data.scout_team);
        this.displayStatistics(stats);
    },

    /**
     * Calculate team statistics
     */
    calculateStatistics(players) {
        const totalPoints = players.reduce((sum, player) => {
            return sum + (parseFloat(player.expected_points) || 0);
        }, 0);

        const captain = players.find(p => p.role === "captain");
        const vice = players.find(p => p.role === "vice");

        return {
            totalPoints: totalPoints.toFixed(2),
            playerCount: players.length,
            captainPoints: captain ? parseFloat(captain.expected_points).toFixed(2) : '-',
            vicePoints: vice ? parseFloat(vice.expected_points).toFixed(2) : '-'
        };
    },

    /**
     * Display calculated statistics
     */
    displayStatistics(stats) {
        if (domElements.totalPoints) {
            domElements.totalPoints.textContent = stats.totalPoints;
        }
        if (domElements.playerCount) {
            domElements.playerCount.textContent = stats.playerCount;
        }
    }
};

// UI State Management
const uiStateManager = {
    /**
     * Show loading state
     */
    showLoading(message = 'Loading team data...') {
        domElements.pitch.innerHTML = `<div class="loading" role="status" aria-live="polite">${message}</div>`;
    },

    /**
     * Show error state
     */
    showError(message) {
        domElements.pitch.innerHTML = `<div class="error" role="alert">${message}</div>`;
    },

    /**
     * Update header information
     */
    updateHeaderAndCredits(data) {
        if (domElements.gameweekInfo && data.gameweek && data.version) {
            domElements.gameweekInfo.textContent = `Gameweek ${data.gameweek} • ${data.version}`;
        }
        if (domElements.credits && data.credits) {
            domElements.credits.textContent = data.credits;
        }
    },

    /**
     * Set loading state for buttons
     */
    setButtonLoading(button, isLoading, loadingText = 'Loading...', originalText = '') {
        if (isLoading) {
            button.disabled = true;
            button.dataset.originalText = button.textContent;
            button.textContent = loadingText;
        } else {
            button.disabled = false;
            button.textContent = originalText || button.dataset.originalText || 'Complete';
        }
    }
};

// Data Loading
const dataLoader = {
    /**
     * Load data from JSON file
     */
    async loadDataFromFile(gameweek) {
        const filePath = `${CONFIG.dataPath}${CONFIG.filePrefix}${gameweek}${CONFIG.fileExtension}`;

        try {
            uiStateManager.showLoading(`Loading Gameweek ${gameweek} data...`);

            const response = await fetch(filePath);
            if (!response.ok) {
                throw new Error(`Failed to load ${filePath}: ${response.status} ${response.statusText}`);
            }

            const data = await utils.safeJsonParse(response);

            // Validate data structure
            if (!data.scout_team || !Array.isArray(data.scout_team)) {
                throw new Error('Invalid data format: missing scout_team array');
            }

            return data;

        } catch (error) {
            console.error('Error loading JSON data:', error);
            throw error;
        }
    },

    /**
     * Discover available gameweeks
     */
    async discoverGameweeks() {
        const gameweeks = [];
        const promises = [];

        for (let gw = 1; gw <= CONFIG.maxGameweeks; gw++) {
            const filePath = `${CONFIG.dataPath}${CONFIG.filePrefix}${gw}${CONFIG.fileExtension}`;
            promises.push(
                fetch(filePath, { method: 'HEAD' })
                    .then(response => response.ok ? gw : null)
                    .catch(() => null)
            );
        }

        const results = await Promise.all(promises);
        return results.filter(gw => gw !== null).sort((a, b) => a - b);
    }
};

// Gameweek Management
const gameweekManager = {
    /**
     * Populate gameweek selector dropdown
     */
    populateGameweekSelector(gameweeks) {
        const selector = domElements.gameweekSelect;
        if (!selector) return;

        selector.innerHTML = '';

        if (gameweeks.length === 0) {
            selector.innerHTML = '<option value="">No gameweeks available</option>';
            return;
        }

        gameweeks.forEach(gw => {
            const option = document.createElement('option');
            option.value = gw;
            option.textContent = `Gameweek ${gw}`;
            if (gw === appState.currentGameweek) {
                option.selected = true;
            }
            selector.appendChild(option);
        });
    },

    /**
     * Handle gameweek selection change
     */
    async handleGameweekChange(gameweek) {
        const newGameweek = parseInt(gameweek);
        if (!newGameweek || newGameweek === appState.currentGameweek || appState.isLoading) {
            return;
        }

        appState.currentGameweek = newGameweek;
        await this.loadAndDisplayData(newGameweek);
    },

    /**
     * Load and display data for specific gameweek
     */
    async loadAndDisplayData(gameweek) {
        if (appState.isLoading) return;

        try {
            appState.isLoading = true;
            const data = await dataLoader.loadDataFromFile(gameweek);
            
            appState.currentData = data;
            teamRenderer.renderTeam(data);
            statisticsManager.updateStatistics(data);
            uiStateManager.updateHeaderAndCredits(data);
            
        } catch (error) {
            const errorMessage = `
                <strong>Error loading Gameweek ${gameweek} data</strong><br>
                ${error.message}<br><br>
                <small>Make sure the file <code>${CONFIG.dataPath}${CONFIG.filePrefix}${gameweek}${CONFIG.fileExtension}</code> exists and is accessible.</small>
            `;
            uiStateManager.showError(errorMessage);
        } finally {
            appState.isLoading = false;
        }
    }
};

// Screenshot Functionality
const screenshotManager = {
    /**
     * Take screenshot of the team display
     */
    async takeScreenshot() {
        const button = domElements.screenshotBtn;
        const container = domElements.container;

        if (!button || !container) return;

        try {
            uiStateManager.setButtonLoading(button, true, '📸 Capturing...');

            // Wait for UI update
            await new Promise(resolve => setTimeout(resolve, 100));

            // Scroll to top for proper positioning
            window.scrollTo({ top: 0, behavior: 'smooth' });

            // Wait for scroll to complete
            await new Promise(resolve => setTimeout(resolve, 300));

            if (typeof html2canvas === 'undefined') {
                throw new Error('Screenshot library not available');
            }

            const canvas = await html2canvas(container, this.getScreenshotOptions(container));
            this.downloadScreenshot(canvas);

            uiStateManager.setButtonLoading(button, false, '✅ Downloaded!');
            setTimeout(() => {
                uiStateManager.setButtonLoading(button, false, '📸 Screenshot');
            }, 2000);

        } catch (error) {
            console.error('Screenshot failed:', error);
            uiStateManager.setButtonLoading(button, false, '❌ Failed');
            
            setTimeout(() => {
                uiStateManager.setButtonLoading(button, false, '📸 Screenshot');
            }, 2000);

            // Fallback to print dialog
            this.fallbackScreenshot();
        }
    },

    /**
     * Get screenshot configuration options
     */
    getScreenshotOptions(container) {
        return {
            backgroundColor: '#667eea',
            scale: 2,
            useCORS: true,
            allowTaint: true,
            logging: false,
            width: container.offsetWidth,
            height: container.offsetHeight,
            x: 0,
            y: 0,
            scrollX: 0,
            scrollY: 0,
            windowWidth: window.innerWidth,
            windowHeight: window.innerHeight
        };
    },

    /**
     * Download the screenshot
     */
    downloadScreenshot(canvas) {
        const link = document.createElement('a');
        const date = new Date().toISOString().split('T')[0];
        link.download = `FPL-Scout-Team-GW${appState.currentGameweek}-${date}.png`;
        link.href = canvas.toDataURL('image/png', 1.0);

        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    },

    /**
     * Fallback screenshot method
     */
    fallbackScreenshot() {
        if (window.print) {
            window.print();
        } else {
            alert('Screenshot feature not supported. Please use your device\'s built-in screenshot function.');
        }
    }
};

// Event Handlers
const eventHandlers = {
    /**
     * Handle player card clicks/interactions
     */
    handlePlayerCardClick(event) {
        const playerCard = event.target.closest('.player-card');
        if (!playerCard) return;

        try {
            const playerData = JSON.parse(playerCard.dataset.player);
            this.showPlayerInfo(playerData);
        } catch (error) {
            console.error('Error parsing player data:', error);
        }
    },

    /**
     * Handle keyboard navigation for player cards
     */
    handlePlayerCardKeydown(event) {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            this.handlePlayerCardClick(event);
        }
    },

    /**
     * Show player information modal/alert
     */
    showPlayerInfo(playerData) {
        const message = `
Player: ${playerData.web_name}
Team: ${playerData.team_name}
Position: ${playerData.element_type}
Fixture: ${playerData.team_name} vs ${playerData.opponent_team_name} (${playerData.was_home ? 'Home' : 'Away'})
Expected Points: ${playerData.expected_points.toFixed(2)}${playerData.role ? `\nRole: ${playerData.role.charAt(0).toUpperCase() + playerData.role.slice(1)}` : ''}
        `.trim();
        
        alert(message);
    },

    /**
     * Handle gameweek selector change
     */
    handleGameweekChange: utils.debounce(async (event) => {
        await gameweekManager.handleGameweekChange(event.target.value);
    }, 300)
};

// Event Listeners Setup
const eventListeners = {
    /**
     * Initialize all event listeners
     */
    init() {
        // Player card interactions
        document.addEventListener('click', eventHandlers.handlePlayerCardClick);
        document.addEventListener('keydown', eventHandlers.handlePlayerCardKeydown);

        // Gameweek selector
        const gameweekSelect = domElements.gameweekSelect;
        if (gameweekSelect) {
            gameweekSelect.addEventListener('change', eventHandlers.handleGameweekChange);
        }

        // Screenshot button
        const screenshotBtn = domElements.screenshotBtn;
        if (screenshotBtn) {
            screenshotBtn.addEventListener('click', () => screenshotManager.takeScreenshot());
        }

        // Keyboard accessibility for screenshot
        if (screenshotBtn) {
            screenshotBtn.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    screenshotManager.takeScreenshot();
                }
            });
        }
    }
};

// Application Initialization
const app = {
    /**
     * Initialize the application
     */
    async init() {
        try {
            // Set up event listeners
            eventListeners.init();

            // Check screenshot library availability
            this.checkScreenshotLibrary();

            // Discover available gameweeks
            uiStateManager.showLoading('Discovering available gameweeks...');
            appState.availableGameweeks = await dataLoader.discoverGameweeks();

            if (appState.availableGameweeks.length === 0) {
                const errorMessage = `
                    <strong>No gameweek data found</strong><br>
                    Please ensure JSON files exist in the <code>${CONFIG.dataPath}</code> directory<br>
                    Expected format: <code>${CONFIG.filePrefix}1${CONFIG.fileExtension}</code>, <code>${CONFIG.filePrefix}2${CONFIG.fileExtension}</code>, etc.
                `;
                uiStateManager.showError(errorMessage);
                return;
            }

            // Set current gameweek to latest available if default is not available
            if (!appState.availableGameweeks.includes(appState.currentGameweek)) {
                appState.currentGameweek = Math.max(...appState.availableGameweeks);
            }

            // Populate gameweek selector
            gameweekManager.populateGameweekSelector(appState.availableGameweeks);

            // Load and display data for current gameweek
            await gameweekManager.loadAndDisplayData(appState.currentGameweek);

        } catch (error) {
            console.error('Error initializing application:', error);
            const errorMessage = `
                <strong>Failed to initialize application</strong><br>
                ${error.message}<br><br>
                <small>Please check the console for more details.</small>
            `;
            uiStateManager.showError(errorMessage);
        }
    },

    /**
     * Check if screenshot library is available
     */
    checkScreenshotLibrary() {
        if (typeof html2canvas === 'undefined') {
            console.warn('html2canvas library not loaded, screenshot functionality will use fallback method');
        }
    }
};

// Start the application when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    app.init();
});

// Export for potential testing or external access
window.FPLScoutApp = {
    app,
    appState,
    CONFIG,
    utils,
    teamRenderer,
    screenshotManager,
    gameweekManager
};
