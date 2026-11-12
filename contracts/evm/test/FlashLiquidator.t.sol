// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "../src/FlashLiquidator.sol";
import "./Mocks.sol";

contract FlashLiquidatorTest {
    FlashLiquidator public liquidator;
    MockBalancerVault public vault;
    MockSwapRouter public router;
    MockERC20 public usdc;
    MockERC20 public weth;
    MockCToken public mUsdc;
    MockCToken public mWeth;

    address public owner = address(this);
    address public borrower = address(0xB0B);
    address public attacker = address(0xBAD);

    function setUp() public {
        vault = new MockBalancerVault();
        router = new MockSwapRouter();
        
        usdc = new MockERC20("USD Coin", "USDC", 6);
        weth = new MockERC20("Wrapped Ether", "WETH", 18);

        mUsdc = new MockCToken(address(usdc));
        mWeth = new MockCToken(address(weth));

        liquidator = new FlashLiquidator(address(vault), address(router));

        // Fund vault with USDC to provide flash loans
        usdc.mint(address(vault), 1_000_000 * 1e6);
    }

    function testSuccessfulLiquidationCycle() public {
        uint256 repayAmount = 10_000 * 1e6; // $10,000 USDC
        uint256 swapProceeds = 10_500 * 1e6; // $10,500 USDC ($500 net profit)
        uint256 minProfit = 100 * 1e6; // $100 min profit required

        router.setProceeds(swapProceeds);

        FlashLiquidator.LiquidationParams memory params = FlashLiquidator.LiquidationParams({
            borrower: borrower,
            cTokenBorrowed: address(mUsdc),
            cTokenCollateral: address(mWeth),
            debtToken: address(usdc),
            collateralUnderlying: address(weth),
            repayAmount: repayAmount,
            minProfitDebtUnits: minProfit,
            swapPoolFee: 3000,
            swapMinAmountOut: 10_100 * 1e6,
            deadline: block.timestamp + 300
        });

        liquidator.executeFlashLiquidation(params);

        // Verify liquidator retained net profit
        uint256 finalBalance = usdc.balanceOf(address(liquidator));
        assert(finalBalance == 500 * 1e6);

        // Verify allowances are wiped to 0
        assert(usdc.allowance(address(liquidator), address(mUsdc)) == 0);
        assert(weth.allowance(address(liquidator), address(router)) == 0);
    }

    function testAdversarialCallbackSpoofingFails() public {
        // Attacker attempts to call receiveFlashLoan directly
        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(address(usdc));
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 1000;
        uint256[] memory feeAmounts = new uint256[](1);
        feeAmounts[0] = 0;

        (bool success, ) = address(liquidator).call(
            abi.encodeWithSelector(
                IFlashLoanRecipient.receiveFlashLoan.selector,
                tokens,
                amounts,
                feeAmounts,
                ""
            )
        );
        // Must fail because msg.sender != trustedVault
        assert(!success);
    }

    function testRevertWhenProfitFloorUnmet() public {
        uint256 repayAmount = 10_000 * 1e6;
        uint256 swapProceeds = 10_010 * 1e6; // Only $10 profit
        uint256 minProfit = 50 * 1e6; // Demands $50 profit

        router.setProceeds(swapProceeds);

        FlashLiquidator.LiquidationParams memory params = FlashLiquidator.LiquidationParams({
            borrower: borrower,
            cTokenBorrowed: address(mUsdc),
            cTokenCollateral: address(mWeth),
            debtToken: address(usdc),
            collateralUnderlying: address(weth),
            repayAmount: repayAmount,
            minProfitDebtUnits: minProfit,
            swapPoolFee: 3000,
            swapMinAmountOut: 10_000 * 1e6,
            deadline: block.timestamp + 300
        });

        (bool success, ) = address(liquidator).call(
            abi.encodeWithSelector(liquidator.executeFlashLiquidation.selector, params)
        );
        // Must revert due to InsufficientProfit
        assert(!success);
    }

    function testBalanceSnapshottingPreventsMaskedLoss() public {
        // Give liquidator pre-existing balance of $1,000 USDC
        usdc.mint(address(liquidator), 1_000 * 1e6);

        uint256 repayAmount = 10_000 * 1e6;
        // Swap proceeds are only $9,800 (a $200 loss!)
        // Even though 1000 + 9800 = 10800 > 10000, finalBalance (800) < initialBalance (1000)
        uint256 swapProceeds = 9_800 * 1e6;
        uint256 minProfit = 10 * 1e6;

        router.setProceeds(swapProceeds);

        FlashLiquidator.LiquidationParams memory params = FlashLiquidator.LiquidationParams({
            borrower: borrower,
            cTokenBorrowed: address(mUsdc),
            cTokenCollateral: address(mWeth),
            debtToken: address(usdc),
            collateralUnderlying: address(weth),
            repayAmount: repayAmount,
            minProfitDebtUnits: minProfit,
            swapPoolFee: 3000,
            swapMinAmountOut: 9_000 * 1e6,
            deadline: block.timestamp + 300
        });

        (bool success, ) = address(liquidator).call(
            abi.encodeWithSelector(liquidator.executeFlashLiquidation.selector, params)
        );
        // Must revert because pre-existing balance cannot cover the deficit!
        assert(!success);
    }

    function testUnauthorizedWithdrawReverts() public {
        usdc.mint(address(liquidator), 500 * 1e6);

        // Attacker caller contract attempts to withdraw
        NonOwnerCaller nonOwner = new NonOwnerCaller();
        bool callSucceeded = nonOwner.tryWithdraw(address(liquidator), address(usdc), attacker);
        assert(!callSucceeded);

        // Owner can withdraw successfully
        liquidator.withdraw(address(usdc), owner);
        assert(usdc.balanceOf(address(liquidator)) == 0);
        assert(usdc.balanceOf(owner) == 500 * 1e6);
    }
}

contract NonOwnerCaller {
    function tryWithdraw(address liquidator, address token, address recipient) external returns (bool) {
        (bool success, ) = liquidator.call(
            abi.encodeWithSignature("withdraw(address,address)", token, recipient)
        );
        return success;
    }
}
