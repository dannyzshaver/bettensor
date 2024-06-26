from argparse import ArgumentParser
from typing import Tuple
import sys
import bittensor as bt
import sqlite3
from bettensor.base.neuron import BaseNeuron
from bettensor.protocol import Metadata, GameData, TeamGamePrediction
from bettensor.utils.sign_and_validate import verify_signature
from bettensor.utils.miner_stats import MinerStatsHandler
import datetime
import os


class BettensorMiner(BaseNeuron):
    """
    The BettensorMiner class contains all of the code for a Miner neuron

    Attributes:
        neuron_config:
            This attribute holds the configuration settings for the neuron:
            bt.subtensor, bt.wallet, bt.logging & bt.axon
        miner_set_weights:
            A boolean attribute that determines whether the miner sets weights.
            This is set based on the command-line argument args.miner_set_weights.
        wallet:
            Represents an instance of bittensor.wallet returned from the setup() method.
        subtensor:
            An instance of bittensor.subtensor returned from the setup() method.
        metagraph:
            An instance of bittensor.metagraph returned from the setup() method.
        miner_uid:
            An int instance representing the unique identifier of the miner in the network returned
            from the setup() method.
        hotkey_blacklisted:
            A boolean flag indicating whether the miner's hotkey is blacklisted.

    """

    default_db_path = "./data/miner.db"

    def __init__(self, parser: ArgumentParser):
        """
        Initializes the Miner class.

        Arguments:
            parser:
                An ArgumentParser instance.

        Returns:
            None
        """
        super().__init__(parser=parser, profile="miner")

        # Allow user to specify db path, if they want to.
        parser.add_argument("--db_path", type=str, default=self.default_db_path)

        # Neuron configuration
        self.neuron_config = self.config(
            bt_classes=[bt.subtensor, bt.logging, bt.wallet, bt.axon]
        )

        args = parser.parse_args()

        self.db_path = args.db_path

        # TODO If users want to run a dual miner/vali. Not fully implemented yet.
        if args.miner_set_weights == "False":
            self.miner_set_weights = False
        else:
            self.miner_set_weights = True

        # Minimum stake for validator whitelist
        self.validator_min_stake = args.validator_min_stake

        # Neuron setup
        self.wallet, self.subtensor, self.metagraph, self.miner_uid = self.setup()
        self.hotkey_blacklisted = False

        # set env variables for Miner process, so that CLI can connect.
        os.environ["DB_PATH"] = self.db_path
        os.environ["HOTKEY"] = self.wallet.hotkey.ss58_address
        os.environ["UID"] = str(self.miner_uid)

        with open("data/miner_env.txt", "a") as f:
            f.write(
                f"UID={self.miner_uid}, DB_PATH={self.db_path}, HOTKEY={self.wallet.hotkey.ss58_address}\n"
            )

        # Initialize local sqlite
        self.ensure_db_directory_exists(self.db_path)
        self.initialize_database()

        # Initialize Miner Stats
        self.stats = MinerStatsHandler(db_path=self.db_path, profile="miner")
        bt.logging.debug(
            f"Miner stats initialized with db_path: {self.db_path} and profile: miner"
        )
        init_stats = self.stats.init_miner_row(
            self.wallet.hotkey.ss58_address, self.miner_uid
        )
        if not init_stats:
            bt.logging.error(
                f"Failed to initialize miner stats. Submitting predictions will not work properly"
            )

        bt.logging.debug(f"init_stats: {init_stats}")

    def ensure_db_directory_exists(self, db_path):
        db_dir = os.path.dirname(db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)

    def print_table_schema(self):
        db, cursor = self.get_cursor()
        cursor.execute("PRAGMA table_info(games)")
        schema = cursor.fetchall()
        for column in schema:
            print(column)
        db.close()

    def initialize_database(self):
        bt.logging.debug(f"Initializing database at {self.db_path}")
        try:
            db = sqlite3.connect(self.db_path)
            cursor = db.cursor()
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS predictions (
                               predictionID TEXT PRIMARY KEY, 
                               teamGameID TEXT, 
                               minerID TEXT, 
                               predictionDate TEXT, 
                               predictedOutcome TEXT,
                               teamA TEXT,
                               teamB TEXT,
                               wager REAL,
                               teamAodds REAL,
                               teamBodds REAL,
                               tieOdds REAL,
                               canOverwrite BOOLEAN,
                               outcome TEXT
                               )"""
            )
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS games (
                               gameID TEXT PRIMARY KEY, 
                               teamA TEXT,
                               teamAodds REAL,
                               teamB TEXT,
                               teamBodds REAL,
                               sport TEXT, 
                               league TEXT, 
                               externalID TEXT, 
                               createDate TEXT, 
                               lastUpdateDate TEXT, 
                               eventStartDate TEXT, 
                               active INTEGER, 
                               outcome TEXT,
                               tieOdds REAL,
                               canTie BOOLEAN
                               )"""
            )
            db.commit()
            db.close()
        except sqlite3.Error as e:
            bt.logging.error(f"Failed to initialize local database: {e}")
            raise Exception("Failed to initialize local database")

    def get_cursor(self):
        try:
            db = sqlite3.connect(self.db_path)
            return db, db.cursor()
        except sqlite3.Error as e:
            bt.logging.error(f"Failed to connect to local database: {e}")
            raise Exception("Failed to connect to local database")

    def setup(self) -> Tuple[bt.wallet, bt.subtensor, bt.metagraph, str]:
        """This function sets up the neuron.

        The setup function initializes the neuron by registering the
        configuration.

        Arguments:
            None

        Returns:
            wallet:
                An instance of bittensor.wallet containing information about
                the wallet
            subtensor:
                An instance of bittensor.subtensor
            metagraph:
                An instance of bittensor.metagraph
            miner_uid:
                An instance of int consisting of the miner UID

        Raises:
            AttributeError:
                The AttributeError is raised if wallet, subtensor & metagraph cannot be logged.
        """
        bt.logging(config=self.neuron_config, logging_dir=self.neuron_config.full_path)
        bt.logging.info(
            f"Initializing miner for subnet: {self.neuron_config.netuid} on network: {self.neuron_config.subtensor.chain_endpoint} with config:\n {self.neuron_config}"
        )

        # Setup the bittensor objects
        try:
            wallet = bt.wallet(config=self.neuron_config)
            subtensor = bt.subtensor(config=self.neuron_config)
            metagraph = subtensor.metagraph(self.neuron_config.netuid)
        except AttributeError as e:
            bt.logging.error(f"Unable to setup bittensor objects: {e}")
            sys.exit()

        bt.logging.info(
            f"Bittensor objects initialized:\nMetagraph: {metagraph}\
            \nSubtensor: {subtensor}\nWallet: {wallet}"
        )

        # Validate that our hotkey can be found from metagraph
        if wallet.hotkey.ss58_address not in metagraph.hotkeys:
            bt.logging.error(
                f"Your miner: {wallet} is not registered to chain connection: {subtensor}. Run btcli register and try again"
            )
            sys.exit()

        # Get the unique identity (UID) from the network
        miner_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
        bt.logging.info(f"Miner is running with UID: {miner_uid}")

        return wallet, subtensor, metagraph, miner_uid

    def check_whitelist(self, hotkey):
        """
        Checks if a given validator hotkey has been whitelisted.

        Arguments:
            hotkey:
                A str instance depicting a hotkey.

        Returns:
            True:
                True is returned if the hotkey is whitelisted.
            False:
                False is returned if the hotkey is not whitelisted.
        """

        if isinstance(hotkey, bool) or not isinstance(hotkey, str):
            return False

        whitelisted_hotkeys = []

        if hotkey in whitelisted_hotkeys:
            return True

        return False

    def blacklist(self, synapse: GameData) -> Tuple[bool, str]:
        """
        This function is executed before the synapse data has been
        deserialized.

        On a practical level this means that whatever blacklisting
        operations we want to perform, it must be done based on the
        request headers or other data that can be retrieved outside of
        the request data.

        As it currently stands, we want to blacklist requests that are
        not originating from valid validators. This includes:
        - unregistered hotkeys
        - entities which are not validators
        - entities with insufficient stake

        Returns:
            [True, ""] for blacklisted requests where the reason for
            blacklisting is contained in the quotes.
            [False, ""] for non-blacklisted requests, where the quotes
            contain a formatted string (f"Hotkey {synapse.dendrite.hotkey}
            has insufficient stake: {stake}",)
        """

        # Check whitelisted hotkeys (queries should always be allowed)
        if self.check_whitelist(hotkey=synapse.dendrite.hotkey):
            bt.logging.info(f"Accepted whitelisted hotkey: {synapse.dendrite.hotkey})")
            return (False, f"Accepted whitelisted hotkey: {synapse.dendrite.hotkey}")

        # Blacklist entities that have not registered their hotkey
        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            bt.logging.info(f"Blacklisted unknown hotkey: {synapse.dendrite.hotkey}")
            return (
                True,
                f"Hotkey {synapse.dendrite.hotkey} was not found from metagraph.hotkeys",
            )

        # Blacklist entities that are not validators
        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        print("uid:", uid)
        print("metagraph", self.metagraph)
        if not self.metagraph.validator_permit[uid]:
            bt.logging.info(f"Blacklisted non-validator: {synapse.dendrite.hotkey}")
            return (True, f"Hotkey {synapse.dendrite.hotkey} is not a validator")

        # Blacklist entities that have insufficient stake
        stake = float(self.metagraph.S[uid])
        if stake < self.validator_min_stake:
            bt.logging.info(
                f"Blacklisted validator {synapse.dendrite.hotkey} with insufficient stake: {stake}"
            )
            return (
                True,
                f"Hotkey {synapse.dendrite.hotkey} has insufficient stake: {stake}",
            )

        # Allow all other entities
        bt.logging.info(
            f"Accepted hotkey: {synapse.dendrite.hotkey} (UID: {uid} - Stake: {stake})"
        )
        return (False, f"Accepted hotkey: {synapse.dendrite.hotkey}")

    def priority(self, synapse: GameData) -> float:
        """
        Assigns a priority to the synapse based on the stake of the validator.
        """

        # Prioritize whitelisted validators
        if self.check_whitelist(hotkey=synapse.dendrite.hotkey):
            return 10000000.0

        # Otherwise prioritize validators based on their stake
        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        stake = float(self.metagraph.S[uid])

        print(f"Prioritized: {synapse.dendrite.hotkey} (UID: {uid} - Stake: {stake})")

        return stake

    def forward(self, synapse: GameData) -> GameData:
        bt.logging.info(f"Miner: forward()")
        db, cursor = self.get_cursor()

        # Print version information and perform version checks
        print(
            f"Synapse version: {synapse.metadata.subnet_version}, our version: {self.subnet_version}"
        )
        if synapse.metadata.subnet_version > self.subnet_version:
            bt.logging.warning(
                f"Received a synapse from a validator with higher subnet version ({synapse.subnet_version}) than yours ({self.subnet_version}). Please update the miner, or you may encounter issues."
            )

        # Verify schema
        # self.print_table_schema()

        # TODO: METADATA / Signature Verification

        game_data_dict = synapse.gamedata_dict
        self.add_game_data(game_data_dict)

        # check if tables in db are initialized
        # if not, initialize them

        cursor.execute("SELECT * FROM games")
        if not cursor.fetchone():
            self.initialize_database()

        # clean up games table and set active field
        self.update_games_data(db, cursor)

        # Get current time
        current_time = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="minutes"
        )

        # Fetch games that have not started yet
        cursor.execute(
            "SELECT externalID FROM games WHERE eventStartDate > ?", (current_time,)
        )
        games = cursor.fetchall()

        bt.logging.debug(f"Fetched {len(games)} games")

        # Log the fetched games
        # bt.logging.info(f"Fetched games: {games}")

        # Process the fetched games
        bt.logging.info(f"Processing predictions")
        prediction_dict = {}
        for game in games:
            bt.logging.debug(f" Processing Predictions: Game: {game}")
            external_game_id = game[0]
            bt.logging.debug(f"Processing Predictions: Game ID: {external_game_id}")
            # Fetch predictions for the game
            cursor.execute(
                "SELECT * FROM predictions WHERE teamGameID = ?", (external_game_id,)
            )
            predictions = cursor.fetchall()
            bt.logging.info(f"Predictions: {predictions}")

            # Add predictions to prediction_dict
            for prediction in predictions:
                single_prediction = TeamGamePrediction(
                    predictionID=prediction[0],
                    teamGameID=prediction[1],
                    minerID=str(self.miner_uid),
                    predictionDate=prediction[3],
                    predictedOutcome=prediction[4],
                    teamA=prediction[5],
                    teamB=prediction[6],
                    wager=prediction[7],
                    teamAodds=prediction[8],
                    teamBodds=prediction[9],
                    tieOdds=prediction[10],
                    can_overwrite=prediction[11],
                    outcome=prediction[12],
                )
                prediction_dict[prediction[0]] = single_prediction

        bt.logging.info(f"prediction_dict: {prediction_dict}")
        synapse.prediction_dict = prediction_dict
        synapse.gamedata_dict = None
        synapse.metadata = Metadata.create(
            wallet=self.wallet,
            subnet_version=self.subnet_version,
            neuron_uid=self.miner_uid,
            synapse_type="prediction",
        )
        return synapse

    def add_game_data(self, game_data_dict):
        try:
            bt.logging.debug(f"add_game_data() | Adding game data to local database")
            db, cursor = self.get_cursor()
            number_of_games = len(game_data_dict)
            bt.logging.debug(
                f"add_game_data() | Number of games to add: {number_of_games}"
            )
            # Check games table, add games that are not in the table
            for game_id, game_data in game_data_dict.items():
                cursor.execute("SELECT * FROM games WHERE gameID = ?", (game_id,))
                if not cursor.fetchone():
                    # Game is not in the table, add it
                    cursor.execute(
                        """INSERT INTO games (
                        gameID, teamA, teamAodds, teamB, teamBodds, sport, league, externalID, createDate, lastUpdateDate, 
                        eventStartDate, active, outcome, tieOdds, canTie
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            game_id,
                            game_data.teamA,
                            game_data.teamAodds,
                            game_data.teamB,
                            game_data.teamBodds,
                            game_data.sport,
                            game_data.league,
                            game_data.externalId,
                            game_data.createDate,
                            game_data.lastUpdateDate,
                            game_data.eventStartDate,
                            game_data.active,
                            game_data.outcome,
                            game_data.tieOdds,
                            game_data.canTie,
                        ),
                    )
                    db.commit()
                else:
                    bt.logging.debug(
                        f"add_game_data() | Game {game_id} already in the table, updating."
                    )
                    cursor.execute(
                        """UPDATE games SET teamA = ?, teamAodds = ?, teamB = ?, teamBodds = ?, sport = ?, league = ?, externalID = ?, 
                                   createDate = ?, lastUpdateDate = ?, eventStartDate = ?, active = ?, outcome = ?, tieOdds = ?, canTie = ? WHERE gameID = ?""",
                        (
                            game_data.teamA,
                            game_data.teamAodds,
                            game_data.teamB,
                            game_data.teamBodds,
                            game_data.sport,
                            game_data.league,
                            game_data.externalId,
                            game_data.createDate,
                            game_data.lastUpdateDate,
                            game_data.eventStartDate,
                            game_data.active,
                            game_data.outcome,
                            game_data.tieOdds,
                            game_data.canTie,
                            game_id,
                        ),
                    )
                    db.commit()

        except Exception as e:
            bt.logging.error(f"Failed to add game data: {e}")

    def update_games_data(self, db, cursor):
        bt.logging.info(f"update_games_data() | Updating games data")
        # go through games table. If current time UTC is greater than eventStartDate, set active to 1

        # if current time UTC is 3 days past event start date, remove the row

        cursor.execute("SELECT * FROM games")
        games = cursor.fetchall()
        for game in games:
            bt.logging.debug(
                f"Current time: {datetime.datetime.now(datetime.timezone.utc)}"
            )
            bt.logging.debug(f"update_games_data() | Game: {game}")
            if datetime.datetime.now(
                datetime.timezone.utc
            ) > datetime.datetime.fromisoformat(game[10]):
                cursor.execute(
                    "UPDATE games SET active = 1 WHERE gameID = ?", (game[0],)
                )
                bt.logging.debug(f"update_games_data() | Game {game[0]} is now active")
            if datetime.datetime.now(
                datetime.timezone.utc
            ) > datetime.datetime.fromisoformat(game[10]) + datetime.timedelta(days=3):
                cursor.execute("DELETE FROM games WHERE gameID = ?", (game[0],))
                bt.logging.debug(
                    f"update_games_data() | Game {game[0]} is deleted from db"
                )
            if datetime.datetime.now(
                datetime.timezone.utc
            ) < datetime.datetime.fromisoformat(game[10]):
                cursor.execute(
                    "UPDATE games SET active = 0 WHERE gameID = ?", (game[0],)
                )
                bt.logging.debug(
                    f"update_games_data() | Game {game[0]} is now inactive"
                )

            db.commit()
