package rebook.entity;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * Slim order view for rebook: only the six fields the flow reads.
 */
@Data
@AllArgsConstructor
@NoArgsConstructor
public class Order {

    private String id;

    private String accountId;

    private String trainNumber;

    private String travelDate;

    private int seatClass;

    private String price;

    public boolean isPaid() {
        return price != null && !price.isEmpty();
    }

    public double priceAsDouble() {
        return price == null || price.isEmpty() ? 0.0 : Double.parseDouble(price);
    }
}
