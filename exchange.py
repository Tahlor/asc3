import numpy as np
import tensorflow as tf
import math
from process_data.utils import *

DATA = ".\data\BTC_USD_100_FREQ.npy"
#DATA = ".\data\BTC-USD_VERY_SHORT.npy"

# Observe every state, but only act every few states?
# Delay moves by several states

# other features: weight losses more (to simulate risk aversion)
# use a 2 second delay before transactions to simulate latency
# Does the system need to learn it can't buy more coins if it has none? E.g. we could veto these trades;
# or we could assume it's just saying "if I could buy, I would"; in either case, it will need to learn that
# choosing a buy action without cash doesn't do anything, I think it should

# State
# Use LSTM to remember previous states
# New information is: holdings, cash, price, % change in price, whether it was a market buy/sell
# If using transaction level, can also include size of order

# Transaction costs
# Impose a transaction cost for market orders?
# OR REQUIRE the model to take limit orders
# ACTIONS ARE LIMIT ORDERS

# Instead of policy/value, we know the best move at every instant -- tell it to do that instead


# time_interval - each state is a X second period

class Exchange:
    def __init__(self, data_stream, cash = 10000, holdings = 0, actions = [-1,1], time_interval = None, transaction_cost = 0):

        '''
        Expects a list of dictionaries with the key price
            Can control network behavior with two main parameters:
                1. how long to look back (e.g., past hour, past day, etc.)
                2. how often to sample prices (e.g., get price every minute, get price every hour, etc.)
                3. maybe get order book?
        '''
        # Game parameters
        self.number_of_input_prices_for_basic = 10
        self.number_of_inputs_for_basic = self.number_of_input_prices_for_basic # *2 # for prices and positions
        self.game_length = 1000
        self.naive_sample_pattern = [2**x for x in range(2,10)] # for naive model, which previous prices to look at

        self.data = np.load(data_stream)
        self.vanilla_prices = self.data[:]["price"]
        self.log_prices = np.log(np.copy(self.data[:]["price"].astype("float64")))


        self.state = 0
        self.starting_cash = cash
        self.cash = cash
        self.holdings = holdings
        self.actions = actions
        self.transaction_cost = transaction_cost
        self.price_changes = self.generate_log_prices(1, [0,len(self.data)]) # these are the price changes for the entire exchange
        self.price_change = self.price_changes[0]
        self.permit_short = False
        if not time_interval is None:
            print(self.data[0:30])
            self.generate_prices_at_time(time_interval)
            self.data = self.prices_at_time

    def get_model_input(self, batch_size=1, price_range=None, exogenous=True):
        if price_range is None:
            price_range = [self.state]

        # This can be batched
        if exogenous:
            to_return = []
            for _ in range(batch_size):
                #prices = np.log(self.data[slice(*price_range)]["price"])
                prices = self.generate_log_prices()
                positions = self.data[slice(*price_range)]["side"]

                combined = np.empty([prices.size + positions.size], dtype=prices.dtype)
                combined[0::2] = prices
                combined[1::2] = positions

                to_return.append(combined)

            return np.asarray(to_return)
        else:
            return self.price_change, self.holdings, self.cash, self.data[self.state]["side"]

    def generate_log_prices(self, distance=1, range=None):
        # distance - comparison price; e.g. 5 implies compare this price to the price 5 transactions ago
        # 1 is the previous price
        # create log prices
        # current price - previous price
        if range is None:
            # generate price changes for game
            if self.state > 0:
                # in this case we have to get price one step before the game starts
                # so we can have a valid price change for the first state in the game
                # otherwise this list ends up being GAME_LENGTH - 1 long and things break
                range = [self.state-distance, self.state + self.game_length]
            else:
                # if game starts at beginning of data, we'll insert a 0 later to make the size of the list work out
                range = [self.state, self.state + self.game_length]  # generate price changes for game
        backsteps = min(distance, range[0]) # can't go before beginning of time
        range = [x - backsteps for x in range]
        price_changes = np.log(self.data[slice(*range)]["price"].astype('float64')*1.0)*100 #np.log
        print(type(price_changes))
        price_changes = price_changes[distance:] - price_changes[:-distance]


        price_changes = np.insert(price_changes, 0, [0] * (distance-backsteps)) # no change for first state;

        return price_changes

    def get_next_state(self):
        self.state += 1
        self.current_price = self.data[self.state]["price"]
        self.price_change = self.price_changes[self.state] # use the log price changes
        return self.data[self.state]

    def goto_state(self, state):
        self.state = state
        self.current_price = self.data[self.state]["price"]
        return self.data[self.state]

    def is_terminal_state(self):
        return self.state >= len(self.data)

    # get history - n = how many rows to get, freq = how often to get them
    # this is only good for a single step in one game in the basic model
    def get_price_history_step(self, current_id = None, n = 100, freq=100):
        if current_id is None:
            current_id = n*freq
        elif current_id < n * freq:
            print("Initial trade id must be greater than freq * n")
        return np.copy(self.data[current_id:current_id-(n*freq):-freq]["price"])

    def get_batch_price_indices(self, state_range, freq, backsamples = None):
        if backsamples is None:
            backsamples = self.number_of_input_prices_for_basic

        # Handle a list input - list should be of form [1,4,9] to bring in prices from 4th previous price, 9th previous price, etc.
        if type(freq) == type([]):
            backsamples = len(freq)
            further_back_ref = max(freq)

            # Add a 0 if needed
            if freq[0] != 0:
                pattern = np.insert(0 - np.array(freq), 0, 0)
            else:
                pattern = freq
        else:
            further_back_ref = freq * backsamples # e.g. the earliest price I need
            pattern = np.array(range(0, -further_back_ref - 1, -freq))

        # Pattern will now look like [0, -1, -4 etc.]

        ### further_back_ref needs to be greater than start! ###

        size = state_range[1]-state_range[0]  # size of game
        start = state_range[0]
        end = start + size

        m = range(start - further_back_ref, end)
        x = np.asarray(range(start - further_back_ref, end)) # create an array starting at the earliest index value you need

        # [backsamples-1::-1][::-1]
        z = np.tile(x[pattern + further_back_ref], (size, 1)) + np.tile(np.array(range(0, size))[:, None], backsamples + 1)

        return z

    # This will return a single games worth of steps for the basic model
    # There is no "batching" however
    # Return a [1 (batch size) x seq length x # of input prices)
    def get_price_history(self, prices = None, start = None, range = None, backsamples=None, freq=None, batch= True, calc_diff = True):
        if prices is None:
            prices = self.log_prices
        if range is None and start is None:
            range = [self.state, self.state+self.game_length]
        if freq is None:
            freq = self.game_length/10
        if backsamples is None:
            backsamples = self.number_of_input_prices_for_basic

        if not batch:
            return self.get_price_history_step(self, current_id=start, n=backsamples, freq=freq)
        else:
            # For basic model, return a tensor of previous inputs
            # Array of data

            # Override default freq
            # There are two frequencies -- one is the frequency of previous time steps (e.g. for the naive model)
            # Other frequency is the comparison price for % change calculation - kind of a gradient over that period
            price_indices = self.get_batch_price_indices(range=range, freq=freq, backsamples=backsamples)

            # Constant shift
            #comparison_prices_for_game = self.get_batch_prices(prices=self.log_prices, range=range-freq, freq=freq, backsamples=backsamples)
            # (prices_for_game - comparison_prices_for_game)[:, None]
            # Relative shift:
            if not calc_diff:
                return prices[price_indices]
            else:
                return prices[price_indices][:,0:-1] - prices[price_indices][:,1:]

    def get_model_input_naive(self):
        prices = self.get_price_history(freq=self.naive_sample_pattern, batch= True, )
        buy_sell = self.get_batch_prices(prices=self.data[:]["side"], freq=self.naive_sample_pattern, )[:None] # add a batch dimension
        return np.concatenate((prices,buy_sell), 2) #[1 (batches x seq length x prev_states * 2)] ; 2 is for prices and sides

    # same as above, but can optionally define a list [0,10,50,100] of previous time steps, or a function
    def get_price_history_func(self, current_id = None, n = 100, pattern=lambda x: x**2):
        if type(pattern) == type([]):
            if np.sum(pattern) > 0:
                pattern = -pattern
        else:
            func = pattern
            pattern = []
            for x in range(0,n):
                pattern.append(current_id-func(x))
        return np.copy(self.data[pattern]["price"])

    # look at prices every X seconds (rather than each transaction as a new state)
    def generate_prices_at_time(self, seconds = 60, prices_only = False, interpolation = "repeat"):
        current_time = self.data[0]["time"]
        target = round_to_nearest(current_time, round_by=seconds)
        previous_target = target
        self.prices_at_time = [0]

        for n, i in enumerate(self.data):
            if i["time"] > target:
                target = round_to_nearest(i["time"], seconds)
                time_steps = int((target-previous_target)/seconds ) # number of missing time intervals

                # Return list of prices only or index of complete transactions
                next_item = [n] if not prices_only else [i["price"]]

                # Interpolation if no transactions in interval
                if interpolation == "repeat":
                    self.prices_at_time += [self.prices_at_time[-1]]*time_steps + next_item
                elif interpolation is None:
                    self.prices_at_time += [None] * time_steps + next_item

                previous_target = target
                target += seconds

        self.prices_at_time.pop(0)

        if not prices_only:
            #print(self.prices_at_time[0:30])
            self.prices_at_time = np.copy(self.data[self.prices_at_time])

    def buy_security(self, coin = None, currency = None):
        assert (coin is None) != (currency is None)

        if currency is None:
            cost = min(self.cash, self.current_price * coin)
        else:
            cost = min(self.cash, currency)

        self.cash -= cost
        self.holdings += (cost * (1-self.transaction_cost)) / self.current_price

    def sell_security(self, coin = None, currency = None):
        assert (coin is None) != (currency is None)

        if coin is None:
            proceeds = min(self.holdings*self.current_price, currency) if not self.permit_short else currency
        else:
            proceeds = min(self.holdings*self.current_price, coin*self.current_price) if not self.permit_short else coin*self.current_price

        self.cash += proceeds * (1-self.transaction_cost)
        self.holdings -= proceeds/self.current_price

    def get_balances(self):
        return {"cash":self.cash, "holdings":self.holdings}

    def get_value(self):
        return self.cash + self.holdings*self.current_price

    # maybe feed absolute price and price % change from previous state
    def get_perc_change(self):
        return self.current_price/self.data[self.state-1]["price"]
        

    def interpret_action(self, action, sd, continuous = True):
        # this normalizes action to [min, max]
        if continuous:
            action = 2*(action-np.average(self.actions))/(max(self.actions)-min(self.actions))
            action = self.sample_from_action(action, sd)

        # Margin call
        if self.permit_short and self.get_value() < .1*self.starting_cash and self.holdings < 0:
            # close all negative positions if value < 1000
            self.buy_security(coin=-self.holdings)
            return action

        if action < 0:
            if not self.permit_short:
                self.sell_security(coin = self.holdings * abs(action))
            else: # if agent can short, he can short all but 20% of his initial balance
                self.sell_security(coin=(self.get_value()   -.2*self.starting_cash)/self.current_price * abs(action))
        elif action > 0:
            self.buy_security(currency = self.cash * abs(action))
        return action

    def sample_from_action(self, mean = 0, sd = 1):
        sample = np.random.normal(mean, sd)
        return min(max(sample, -1), 1)


def test_buying_and_selling(myExchange):
    #print(x)
    # action can be a vector -1 = 1
    action = 2*(action-np.average(myExchange.actions))/(max(myExchange.actions)-min(myExchange.actions))
    if action < 0:
        myExchange.sell_security(coin = myExchange.holdings * abs(action))
    elif action > 0:
        myExchange.buy_security(currency = myExchange.cash * abs(action))

def test_getting_prices(myExchange):
    #x = myExchange.get_price_history_func(10000)


    #x = myExchange.generate_log_prices(4, [myExchange.state, myExchange.state + 10])
    #print(x[:10])
    #print(myExchange.data[slice(myExchange.state, myExchange.state + 10)]["price"])

    #print(myExchange.get_price_history(backsamples=1, freq=1))
    print(myExchange.get_model_input_naive())

if __name__ == "__main__":
    np.set_printoptions(formatter={'int_kind': lambda x: "{:0>3d}".format(x)})
    np.set_printoptions(formatter={'float_kind': lambda x: "{0:6.3f}".format(x)})

    # myExchange = Exchange(DATA, time_interval=60)
    myExchange = Exchange(DATA)
    myExchange.state = 10000
    test_getting_prices(myExchange)
    #print(myExchange.get_price_history(n = 1, freq=1))
